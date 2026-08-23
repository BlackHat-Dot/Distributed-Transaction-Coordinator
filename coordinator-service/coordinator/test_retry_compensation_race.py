"""
test_retry_compensation_race.py

Step 3: Regression test proving the TOCTOU fix in retry_compensation.
Step 4: Before/after evidence via _old_retry_compensation_write.
"""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent
from coordinator.saga_log import _set_status_with_event


RETRY_URL = "/transactions/{}/retry-compensation/"

# How long the mock inventory call sleeps. Both threads enter the view and
# reach requests.post() before either finishes, then race to commit.
INVENTORY_SLEEP = 0.25

# Stagger: Thread 2 starts this many seconds after Thread 1, ensuring
# Thread 1 is already inside the sleeping requests.post() mock when Thread 2
# enters the view. This means both threads have read txn_status_at_entry
# before either calls _set_status_with_event — reproducing the TOCTOU window.
THREAD_STAGGER = 0.05


# ─────────────────────────────────────────────────────────────────────────────
# OLD code verbatim — stale snapshot path (no lock, no event)
# ─────────────────────────────────────────────────────────────────────────────

def _old_retry_compensation_write(transaction_id, txn_status_snapshot):
    """
    Verbatim copy of the OLD stale-snapshot filter+update.
    No lock, no event append, no atomicity guarantee.
    """
    updated = Transaction.objects.filter(
        transaction_id=transaction_id,
        status=txn_status_snapshot,
    ).update(
        status=Transaction.Status.COMPENSATED_ON_RETRY,
        updated_at=timezone.now(),
    )
    return updated


# ─────────────────────────────────────────────────────────────────────────────
# Step 3: New code regression test
# ─────────────────────────────────────────────────────────────────────────────

class RetryCompensationRaceNewCodeTests(TransactionTestCase):
    """
    Two concurrent retry_compensation calls with a sleeping inventory mock.

    Thread 1 starts first. Thread 2 starts THREAD_STAGGER seconds later,
    so Thread 1 is already inside requests.post() (sleeping) when Thread 2
    reads txn_status_at_entry. Both see COMPENSATION_FAILED as the entry
    status. Both proceed past requests.post() and race to call
    _set_status_with_event. The SELECT FOR UPDATE serializes them:

    - Thread 1 acquires lock -> commits COMPENSATED_ON_RETRY -> releases.
    - Thread 2 acquires lock -> sees status != expected_status -> returns False
      -> responds "concurrently resolved".

    The assertions check:
    (a) Exactly one winner (COMPENSATED_ON_RETRY response).
    (b) Exactly one loser (concurrently-resolved or any non-winner response).
    (c) Exactly one SagaEvent row (no duplicate, no seq collision).
    (d) DB final status is COMPENSATED_ON_RETRY.
    """

    def test_concurrent_retry_compensation_exactly_one_winner(self):
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.COMPENSATION_FAILED,
            product_id=1, quantity=1, amount=100,
        )
        url = RETRY_URL.format(txn.transaction_id)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": "Inventory released"}

        def sleeping_post(*args, **kwargs):
            # Both threads sleep here simultaneously, then race to commit.
            time.sleep(INVENTORY_SLEEP)
            return mock_resp

        results = []
        errors = []

        def do_retry(stagger):
            time.sleep(stagger)   # stagger: thread 1 gets head start
            client = APIClient()
            try:
                resp = client.post(url, format="json")
                return resp.status_code, dict(resp.data)
            except Exception as exc:
                errors.append(exc)
                return None, None
            finally:
                connection.close()

        with patch("coordinator.views.requests.post", side_effect=sleeping_post):
            t1 = threading.Thread(target=lambda: results.append(do_retry(0)))
            t2 = threading.Thread(target=lambda: results.append(do_retry(THREAD_STAGGER)))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        # (a) No exceptions
        self.assertEqual(errors, [], f"Thread exceptions: {errors}")

        datas = [r[1] for r in results if r and r[1] is not None]
        statuses = [d.get("status") for d in datas]

        # (b) Exactly one COMPENSATED_ON_RETRY
        winners = [d for d in datas if d.get("status") == Transaction.Status.COMPENSATED_ON_RETRY
                   and "message" not in d]
        self.assertEqual(
            len(winners), 1,
            f"Expected exactly 1 winner, got statuses: {statuses}. "
            "SELECT FOR UPDATE is not serializing correctly.",
        )

        # (c) Exactly one non-winner (concurrently resolved or error)
        losers = [d for d in datas if d not in winners]
        self.assertEqual(len(losers), 1, f"Expected exactly 1 loser, got: {datas}")

        # (d) Exactly one SagaEvent — no seq collision
        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertEqual(
            len(events), 1,
            f"Expected 1 SagaEvent, found {len(events)}: {events}",
        )
        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[0].to_status, Transaction.Status.COMPENSATED_ON_RETRY)
        self.assertEqual(events[0].from_status, Transaction.Status.COMPENSATION_FAILED)
        self.assertEqual(events[0].actor, SagaEvent.ACTOR_RETRY)

        # (e) Final DB status
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED_ON_RETRY)


# ─────────────────────────────────────────────────────────────────────────────
# Step 4: OLD code before/after evidence
# ─────────────────────────────────────────────────────────────────────────────

class RetryCompensationRaceOldCodeTests(TransactionTestCase):
    """
    Directly exercises _old_retry_compensation_write (verbatim stale-snapshot
    filter+update) under the same concurrency model.

    Both threads:
      1. Capture the same txn.status snapshot (no lock).
      2. Sleep INVENTORY_SLEEP (simulating network I/O) with stagger.
      3. Execute filter(status=snapshot).update(...) — the racy path.

    What this documents:
    - Under PG READ COMMITTED, the second committer sees the first''s committed
      update via the filter, gets 0 rows updated, and "works" for the status.
    - ZERO SagaEvent rows exist. The entire audit trail is absent.
    - Loser detection is implicit via "0 rows" — not an explicit lock CAS.
    """

    def test_old_code_no_audit_trail(self):
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.COMPENSATION_FAILED,
            product_id=1, quantity=1, amount=100,
        )
        tid = txn.transaction_id
        snapshot = txn.status

        results = []

        def do_old_write(stagger):
            time.sleep(stagger)
            try:
                time.sleep(INVENTORY_SLEEP)  # simulate network I/O
                updated = _old_retry_compensation_write(tid, snapshot)
                return updated
            finally:
                connection.close()

        t1 = threading.Thread(target=lambda: results.append(do_old_write(0)))
        t2 = threading.Thread(target=lambda: results.append(do_old_write(THREAD_STAGGER)))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        txn.refresh_from_db()
        event_count = SagaEvent.objects.filter(transaction=txn).count()

        # Critical: no audit events, ever
        self.assertEqual(
            event_count, 0,
            f"Expected 0 SagaEvents from old code, found {event_count}.",
        )

        print(
            f"\n[OLD CODE] thread results={results}, "
            f"final status={txn.status!r}, saga_events={event_count}. "
            "No audit trail. Loser detected by 0-rows-updated, not by a lock."
        )
