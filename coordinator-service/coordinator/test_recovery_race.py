"""
test_recovery_race.py

Step 3: Inter-process race test between Command._compensate() (recovery sweep)
and retry_compensation() (client request path).
"""

import threading
import time
import uuid
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent
from coordinator.management.commands.recovery_stuck_transactions import Command as RecoveryCommand

RETRY_URL = "/transactions/{}/retry-compensation/"
INVENTORY_SLEEP = 0.25
THREAD_STAGGER = 0.05


class SimulatedInventory:
    """
    Simulates the exact idempotent behavior of inventory-service''s
    release_product view (which uses SELECT FOR UPDATE on Reservation).
    """
    def __init__(self, initial_stock=10, reserved_qty=2):
        self.lock = threading.Lock()
        self.stock = initial_stock - reserved_qty  # e.g., 8
        self.reserved_qty = reserved_qty
        self.released = False
        self.call_count = 0

    def release(self, transaction_id):
        time.sleep(INVENTORY_SLEEP)
        with self.lock:
            self.call_count += 1
            if self.released:
                # Idempotent return without incrementing stock again
                return 200, {
                    "message": "Reservation already released",
                    "transaction_id": str(transaction_id),
                }
            self.stock += self.reserved_qty
            self.released = True
            return 200, {
                "message": "Inventory released",
                "transaction_id": str(transaction_id),
            }


class RecoveryAndRetryCompensationRaceTests(TransactionTestCase):
    """
    Simulates a race between:
    1. The background recovery sweep executing _compensate() after claiming
       a stuck transaction into RECOVERY_IN_PROGRESS.
    2. A client simultaneously calling POST /transactions/<id>/retry-compensation/.

    Both paths interact with the Inventory release endpoint concurrently,
    and both attempt to write terminal status updates to the coordinator DB.
    """

    def test_concurrent_recovery_compensate_and_retry_compensation(self):
        # 1. Setup transaction in RECOVERY_IN_PROGRESS (as if _claim just finished)
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.RECOVERY_IN_PROGRESS,
            product_id=1,
            quantity=2,
            amount=100,
        )
        tid = txn.transaction_id
        url = RETRY_URL.format(tid)

        # 2. Setup simulated idempotent Inventory service
        inventory = SimulatedInventory(initial_stock=10, reserved_qty=2)

        def mock_post(url_arg, *args, **kwargs):
            # Dispatch to simulated inventory release
            tid_arg = kwargs.get("json", {}).get("transaction_id", str(tid))
            status_code, body = inventory.release(tid_arg)
            mock_resp = MagicMock()
            mock_resp.status_code = status_code
            mock_resp.json.return_value = body
            return mock_resp

        results = {}
        errors = []

        def run_recovery():
            try:
                cmd = RecoveryCommand()
                # Run _compensate directly as recovery worker
                cmd._compensate(tid)
                results["recovery"] = "done"
            except Exception as e:
                errors.append(("recovery", e))
            finally:
                connection.close()

        def run_retry(stagger):
            time.sleep(stagger)
            try:
                client = APIClient()
                resp = client.post(url, format="json")
                results["retry"] = (resp.status_code, dict(resp.data))
            except Exception as e:
                errors.append(("retry", e))
            finally:
                connection.close()

        # 3. Launch both concurrently
        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=mock_post), \
             patch("coordinator.views.requests.post", side_effect=mock_post):
            t_rec = threading.Thread(target=run_recovery)
            t_ret = threading.Thread(target=lambda: run_retry(THREAD_STAGGER))
            t_rec.start()
            t_ret.start()
            t_rec.join()
            t_ret.join()

        # (a) Assert no exceptions occurred in either thread
        self.assertEqual(errors, [], f"Thread exceptions: {errors}")

        # (b) Assert inventory was called concurrently (both threads called it),
        # but stock was restored EXACTLY ONCE (idempotency held)
        self.assertGreaterEqual(inventory.call_count, 1)
        self.assertEqual(
            inventory.stock, 10,
            f"Expected stock restored to 10, got {inventory.stock}. "
            "Double-release bug detected!",
        )

        # (c) Assert Transaction.status is in a single unambiguous terminal state
        txn.refresh_from_db()
        self.assertIn(
            txn.status,
            [Transaction.Status.COMPENSATED, Transaction.Status.COMPENSATED_ON_RETRY],
            f"Unexpected final status: {txn.status}",
        )

        # (d) Assert SagaEvent audit log correctness
        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        # We expect:
        # Event 1: RECOVERY_IN_PROGRESS -> COMPENSATING (by recovery)
        # Event 2: COMPENSATING -> COMPENSATED (if recovery won) or COMPENSATED_ON_RETRY (if retry won)
        # Exactly ONE terminal event from COMPENSATING
        self.assertEqual(
            len(events), 2,
            f"Expected exactly 2 events, got {len(events)}: {events}",
        )
        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[0].from_status, Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events[0].to_status, Transaction.Status.COMPENSATING)
        self.assertEqual(events[0].actor, SagaEvent.ACTOR_RECOVERY)

        self.assertEqual(events[1].seq, 2)
        self.assertEqual(events[1].from_status, Transaction.Status.COMPENSATING)
        self.assertEqual(events[1].to_status, txn.status)
        self.assertIn(events[1].actor, [SagaEvent.ACTOR_RECOVERY, SagaEvent.ACTOR_RETRY])
