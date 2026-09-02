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

INVENTORY_SLEEP = 0.25

THREAD_STAGGER = 0.05


def _old_retry_compensation_write(transaction_id, txn_status_snapshot):

    updated = Transaction.objects.filter(
        transaction_id=transaction_id,
        status=txn_status_snapshot,
    ).update(
        status=Transaction.Status.COMPENSATED_ON_RETRY,
        updated_at=timezone.now(),
    )
    return updated



class RetryCompensationRaceNewCodeTests(TransactionTestCase):

    def test_concurrent_retry_compensation_exactly_one_winner(self):
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.COMPENSATION_FAILED,
            seat_ids=[1], quantity=1, amount=100,
        )
        url = RETRY_URL.format(txn.transaction_id)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": "Inventory released"}

        def sleeping_post(*args, **kwargs):
            time.sleep(INVENTORY_SLEEP)
            return mock_resp

        results = []
        errors = []

        def do_retry(stagger):
            time.sleep(stagger)
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


        self.assertEqual(errors, [], f"Thread exceptions: {errors}")

        datas = [r[1] for r in results if r and r[1] is not None]
        statuses = [d.get("status") for d in datas]


        winners = [d for d in datas if d.get("status") == Transaction.Status.COMPENSATED_ON_RETRY
                   and "message" not in d]
        self.assertEqual(
            len(winners), 1,
            f"Expected exactly 1 winner, got statuses: {statuses}. "
            "SELECT FOR UPDATE is not serializing correctly.",
        )


        losers = [d for d in datas if d not in winners]
        self.assertEqual(len(losers), 1, f"Expected exactly 1 loser, got: {datas}")


        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertEqual(
            len(events), 1,
            f"Expected 1 SagaEvent, found {len(events)}: {events}",
        )
        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[0].to_status, Transaction.Status.COMPENSATED_ON_RETRY)
        self.assertEqual(events[0].from_status, Transaction.Status.COMPENSATION_FAILED)
        self.assertEqual(events[0].actor, SagaEvent.ACTOR_RETRY)


        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED_ON_RETRY)



class RetryCompensationRaceOldCodeTests(TransactionTestCase):

    def test_old_code_no_audit_trail(self):
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.COMPENSATION_FAILED,
            seat_ids=[1], quantity=1, amount=100,
        )
        tid = txn.transaction_id
        snapshot = txn.status

        results = []

        def do_old_write(stagger):
            time.sleep(stagger)
            try:
                time.sleep(INVENTORY_SLEEP)
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

        self.assertEqual(
            event_count, 0,
            f"Expected 0 SagaEvents from old code, found {event_count}.",
        )

        print(
            f"\n[OLD CODE] thread results={results}, "
            f"final status={txn.status!r}, saga_events={event_count}. "
            "No audit trail. Loser detected by 0-rows-updated, not by a lock."
        )
