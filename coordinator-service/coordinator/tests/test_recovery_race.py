import threading
import time
import uuid
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent
from coordinator.saga_log import _set_status_with_event
from coordinator.management.commands.recovery_stuck_transactions import Command as RecoveryCommand

RETRY_URL = "/transactions/{}/retry-compensation/"
INVENTORY_SLEEP = 0.25
THREAD_STAGGER = 0.05


class SimulatedInventory:

    def __init__(self, initial_stock=10, reserved_qty=2):
        self.lock = threading.Lock()
        self.stock = initial_stock - reserved_qty  # e.g : 8
        self.reserved_qty = reserved_qty
        self.released = False
        self.call_count = 0

    def release(self, transaction_id):
        time.sleep(INVENTORY_SLEEP)
        with self.lock:
            self.call_count += 1
            if self.released:

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

    def test_concurrent_recovery_compensate_and_retry_compensation(self):

        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.RECOVERY_IN_PROGRESS,
            seat_ids=[1],
            quantity=2,
            amount=100,
        )
        tid = txn.transaction_id
        url = RETRY_URL.format(tid)


        inventory = SimulatedInventory(initial_stock=10, reserved_qty=2)

        def mock_post(url_arg, *args, **kwargs):

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


        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {"status": "NOT_CHARGED"}

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=mock_post), \
             patch("coordinator.views.requests.post", side_effect=mock_post), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", return_value=mock_get_resp), \
             patch("coordinator.views.requests.get", return_value=mock_get_resp), \
             patch("coordinator.recovery.requests.get", return_value=mock_get_resp):
            t_rec = threading.Thread(target=run_recovery)
            t_ret = threading.Thread(target=lambda: run_retry(THREAD_STAGGER))
            t_rec.start()
            t_ret.start()
            t_rec.join()
            t_ret.join()


        self.assertEqual(errors, [], f"Thread exceptions: {errors}")


        self.assertGreaterEqual(inventory.call_count, 1)
        self.assertEqual(
            inventory.stock, 10,
            f"Expected stock restored to 10, got {inventory.stock}. "
            "Double-release bug detected!",
        )


        txn.refresh_from_db()
        self.assertIn(
            txn.status,
            [Transaction.Status.COMPENSATED, Transaction.Status.COMPENSATED_ON_RETRY],
            f"Unexpected final status: {txn.status}",
        )

        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
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


class RecoveryClaimTests(TransactionTestCase):

    def test_claim_creates_saga_event(self):
        from datetime import timedelta
        from django.utils import timezone

        past_time = timezone.now() - timedelta(minutes=10)
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.INVENTORY_RESERVED,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )

        Transaction.objects.filter(pk=txn.pk).update(updated_at=past_time)

        cmd = RecoveryCommand()
        threshold = timezone.now() - timedelta(minutes=5)


        claimed_ids = cmd._claim(Transaction.Status.INVENTORY_RESERVED, threshold)
        self.assertEqual(claimed_ids, [txn.transaction_id])

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.RECOVERY_IN_PROGRESS)


        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[0].from_status, Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[0].to_status, Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events[0].actor, SagaEvent.ACTOR_RECOVERY)


        second_claimed_ids = cmd._claim(Transaction.Status.INVENTORY_RESERVED, threshold)
        self.assertEqual(second_claimed_ids, [])

        events_after = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertEqual(len(events_after), 1, "Duplicate SagaEvent was written on second claim attempt")

    def test_concurrent_claim_exactly_one_winner_and_one_event(self):
        from datetime import timedelta
        from django.utils import timezone
        from concurrent.futures import ThreadPoolExecutor, as_completed

        past_time = timezone.now() - timedelta(minutes=10)
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.STARTED,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )
        Transaction.objects.filter(pk=txn.pk).update(updated_at=past_time)
        threshold = timezone.now() - timedelta(minutes=5)

        results = []

        def do_claim(_):
            cmd = RecoveryCommand()
            try:
                claimed = cmd._claim(Transaction.Status.STARTED, threshold)
                return claimed
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(do_claim, i) for i in range(2)]
            for f in as_completed(futures):
                results.append(f.result())


        winners = [r for r in results if txn.transaction_id in r]
        self.assertEqual(len(winners), 1, f"Expected 1 winner, got: {results}")


        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[0].from_status, Transaction.Status.STARTED)
        self.assertEqual(events[0].to_status, Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events[0].actor, SagaEvent.ACTOR_RECOVERY)

    def test_recover_compensating_resolves_to_compensated(self):
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone

        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.STARTED,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )
        tid = txn.transaction_id
        _set_status_with_event(
            transaction_id=tid,
            to_status=Transaction.Status.COMPENSATING,
            expected_status=Transaction.Status.STARTED,
            actor=SagaEvent.ACTOR_RECOVERY,
        )

        past_time = timezone.now() - timedelta(minutes=10)
        Transaction.objects.filter(transaction_id=tid).update(updated_at=past_time)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": "Inventory released"}

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {"status": "NOT_CHARGED"}

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", return_value=mock_resp), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", return_value=mock_get_resp):
            call_command("recovery_stuck_transactions")

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED)

        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertEqual(len(events), 4)

        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[0].from_status, Transaction.Status.STARTED)
        self.assertEqual(events[0].to_status, Transaction.Status.COMPENSATING)

        self.assertEqual(events[1].seq, 2)
        self.assertEqual(events[1].from_status, Transaction.Status.COMPENSATING)
        self.assertEqual(events[1].to_status, Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events[1].actor, SagaEvent.ACTOR_RECOVERY)

        self.assertEqual(events[2].seq, 3)
        self.assertEqual(events[2].from_status, Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events[2].to_status, Transaction.Status.COMPENSATING)
        self.assertEqual(events[2].actor, SagaEvent.ACTOR_RECOVERY)

        self.assertEqual(events[3].seq, 4)
        self.assertEqual(events[3].from_status, Transaction.Status.COMPENSATING)
        self.assertEqual(events[3].to_status, Transaction.Status.COMPENSATED)
        self.assertEqual(events[3].actor, SagaEvent.ACTOR_RECOVERY)

    def test_recover_compensation_failed_resolves_to_compensated(self):
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone

        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.COMPENSATION_FAILED,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )
        tid = txn.transaction_id

        past_time = timezone.now() - timedelta(minutes=10)
        Transaction.objects.filter(transaction_id=tid).update(updated_at=past_time)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": "Inventory released"}

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {"status": "NOT_CHARGED"}

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", return_value=mock_resp), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", return_value=mock_get_resp):
            call_command("recovery_stuck_transactions")

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED)

        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertEqual(len(events), 3)

        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[0].from_status, Transaction.Status.COMPENSATION_FAILED)
        self.assertEqual(events[0].to_status, Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events[0].actor, SagaEvent.ACTOR_RECOVERY)

        self.assertEqual(events[1].seq, 2)
        self.assertEqual(events[1].from_status, Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events[1].to_status, Transaction.Status.COMPENSATING)
        self.assertEqual(events[1].actor, SagaEvent.ACTOR_RECOVERY)

        self.assertEqual(events[2].seq, 3)
        self.assertEqual(events[2].from_status, Transaction.Status.COMPENSATING)
        self.assertEqual(events[2].to_status, Transaction.Status.COMPENSATED)
        self.assertEqual(events[2].actor, SagaEvent.ACTOR_RECOVERY)

    def test_recover_compensation_failed_downstream_reject_leaves_compensation_failed(self):
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone

        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.COMPENSATION_FAILED,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )
        tid = txn.transaction_id

        past_time = timezone.now() - timedelta(minutes=10)
        Transaction.objects.filter(transaction_id=tid).update(updated_at=past_time)

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json.return_value = {"error": "Release error"}

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {"status": "NOT_CHARGED"}

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", return_value=mock_resp), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", return_value=mock_get_resp):
            call_command("recovery_stuck_transactions")

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATION_FAILED)

        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].to_status, Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events[1].to_status, Transaction.Status.COMPENSATING)
        self.assertEqual(events[2].to_status, Transaction.Status.COMPENSATION_FAILED)
