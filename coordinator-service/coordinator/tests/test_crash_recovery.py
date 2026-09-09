from datetime import timedelta
import uuid
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent


CREATE_URL = "/transactions/?sync=true"
PAYMENT_CHARGE_URL = "http://127.0.0.1:8001/payments/charge/"
INVENTORY_RESERVE_URL = "http://127.0.0.1:8000/seats/reserve/"
INVENTORY_CONFIRM_URL = "http://127.0.0.1:8000/seats/confirm/"
PAYMENT_CHECK_URL = "http://127.0.0.1:8001/payments/check/"
INVENTORY_CHECK_URL = "http://127.0.0.1:8000/reserve/check/"
INVENTORY_RELEASE_URL = "http://127.0.0.1:8000/seats/release/"


class SimulatedProcessDeath(BaseException):
    pass


class CrashRecoveryTests(TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_crash_during_payment_processing_downstream_charged_rolls_forward(self):

        tid = str(uuid.uuid4())

        def mock_post_first_life(url, *args, **kwargs):
            if "reserve" in url:
                mock = MagicMock()
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat reserved", "seat_ids": [1]}
                return mock
            if "charge" in url:
                raise SimulatedProcessDeath("Coordinator process killed mid-charge")
            raise ValueError(f"Unexpected POST url: {url}")

        with patch("coordinator.views.requests.post", side_effect=mock_post_first_life):
            with self.assertRaises(SimulatedProcessDeath):
                self.client.post(
                    CREATE_URL,
                    {"transaction_id": tid, "seat_ids": [1], "quantity": 1, "amount": 500},
                    format="json",
                )

        txn = Transaction.objects.get(transaction_id=tid)
        self.assertEqual(txn.status, Transaction.Status.PAYMENT_PROCESSING)

        past_time = timezone.now() - timedelta(minutes=10)
        Transaction.objects.filter(transaction_id=tid).update(updated_at=past_time)
        
        def mock_get_recovery(url, *args, **kwargs):
            if "payments/check" in url:
                mock = MagicMock()
                mock.status_code = 200
                mock.json.return_value = {"status": "CHARGED"}
                return mock
        mock_post_recovery = MagicMock()
        mock_post_recovery.status_code = 200
        mock_post_recovery.json.return_value = {"message": "Seats confirmed"}

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=mock_get_recovery), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", return_value=mock_post_recovery):
            call_command("recovery_stuck_transactions")


        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPLETED)

        
        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertEqual(len(events), 4)

        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[0].from_status, Transaction.Status.STARTED)
        self.assertEqual(events[0].to_status, Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[0].actor, SagaEvent.ACTOR_REQUEST)

        self.assertEqual(events[1].seq, 2)
        self.assertEqual(events[1].from_status, Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[1].to_status, Transaction.Status.PAYMENT_PROCESSING)
        self.assertEqual(events[1].actor, SagaEvent.ACTOR_REQUEST)

        self.assertEqual(events[2].seq, 3)
        self.assertEqual(events[2].from_status, Transaction.Status.PAYMENT_PROCESSING)
        self.assertEqual(events[2].to_status, Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events[2].actor, SagaEvent.ACTOR_RECOVERY)

        self.assertEqual(events[3].seq, 4)
        self.assertEqual(events[3].from_status, Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events[3].to_status, Transaction.Status.COMPLETED)
        self.assertEqual(events[3].actor, SagaEvent.ACTOR_RECOVERY)
        self.assertEqual(events[3].http_status_code, 200)
        self.assertEqual(events[3].http_payload, {"status": "CHARGED"})

    def test_crash_during_payment_processing_downstream_not_charged_compensates(self):
    
        tid = str(uuid.uuid4())

        def mock_post_first_life(url, *args, **kwargs):
            if "reserve" in url:
                mock = MagicMock()
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat reserved", "seat_ids": [1]}
                return mock
            if "charge" in url:
                raise SimulatedProcessDeath("Coordinator process killed before charge")
            raise ValueError(f"Unexpected POST url: {url}")

       
        with patch("coordinator.views.requests.post", side_effect=mock_post_first_life):
            with self.assertRaises(SimulatedProcessDeath):
                self.client.post(
                    CREATE_URL,
                    {"transaction_id": tid, "seat_ids": [1], "quantity": 1, "amount": 500},
                    format="json",
                )

        txn = Transaction.objects.get(transaction_id=tid)
        self.assertEqual(txn.status, Transaction.Status.PAYMENT_PROCESSING)

        past_time = timezone.now() - timedelta(minutes=10)
        Transaction.objects.filter(transaction_id=tid).update(updated_at=past_time)

        inventory_released = [False]

        def mock_get_recovery(url, *args, **kwargs):
            if "payments/check" in url:
                mock = MagicMock()
                mock.status_code = 200
                mock.json.return_value = {"status": "NOT_CHARGED"}
                return mock
            if "reserve/check" in url:
                mock = MagicMock()
                mock.status_code = 200
                mock.json.return_value = {"status": Transaction.Status.INVENTORY_RESERVED}
                return mock
            raise ValueError(f"Unexpected GET url: {url}")

        def mock_post_recovery(url, *args, **kwargs):
            if "release" in url:
                inventory_released[0] = True
                mock = MagicMock()
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat released"}
                return mock
            raise ValueError(f"Unexpected POST url: {url}")

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=mock_get_recovery), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=mock_post_recovery):
            call_command("recovery_stuck_transactions")

        self.assertTrue(inventory_released[0], "Recovery did not call inventory release endpoint")

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED)

        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertEqual(len(events), 5)

        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[0].from_status, Transaction.Status.STARTED)
        self.assertEqual(events[0].to_status, Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[0].actor, SagaEvent.ACTOR_REQUEST)

        self.assertEqual(events[1].seq, 2)
        self.assertEqual(events[1].from_status, Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[1].to_status, Transaction.Status.PAYMENT_PROCESSING)
        self.assertEqual(events[1].actor, SagaEvent.ACTOR_REQUEST)

        self.assertEqual(events[2].seq, 3)
        self.assertEqual(events[2].from_status, Transaction.Status.PAYMENT_PROCESSING)
        self.assertEqual(events[2].to_status, Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events[2].actor, SagaEvent.ACTOR_RECOVERY)

        self.assertEqual(events[3].seq, 4)
        self.assertEqual(events[3].from_status, Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events[3].to_status, Transaction.Status.COMPENSATING)
        self.assertEqual(events[3].actor, SagaEvent.ACTOR_RECOVERY)

        self.assertEqual(events[4].seq, 5)
        self.assertEqual(events[4].from_status, Transaction.Status.COMPENSATING)
        self.assertEqual(events[4].to_status, Transaction.Status.COMPENSATED)
        self.assertEqual(events[4].actor, SagaEvent.ACTOR_RECOVERY)
        self.assertEqual(events[4].http_status_code, 200)
        self.assertEqual(events[4].http_payload, {"message": "Seat released"})

    def test_recovery_payment_charged_but_inventory_confirm_fails_routes_to_compensate(self):
        tid = str(uuid.uuid4())
        txn = Transaction.objects.create(
            transaction_id=tid,
            status=Transaction.Status.PAYMENT_PROCESSING,
            seat_ids=[1],
            amount=500,
        )
        past_time = timezone.now() - timedelta(minutes=10)
        Transaction.objects.filter(transaction_id=tid).update(updated_at=past_time)

        def mock_get(url, *args, **kwargs):
            if "payments/check" in url:
                mock = MagicMock()
                mock.status_code = 200
                mock.json.return_value = {"status": "CHARGED"}
                return mock
            raise ValueError(f"Unexpected GET: {url}")

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "seats/confirm" in url:
                mock.status_code = 409
                mock.json.return_value = {"error": "Hold expired", "code": "HOLD_EXPIRED"}
                return mock
            if "seats/release" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat released"}
                return mock
            if "payments/refund" in url:
                mock.status_code = 200
                mock.json.return_value = {"status": "REFUNDED", "transaction_id": tid}
                return mock
            raise ValueError(f"Unexpected POST: {url}")

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=mock_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=mock_post):
            call_command("recovery_stuck_transactions")

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED, "Should compensate, NOT complete!")

    def test_recovery_sweeps_timeout_and_connection_error_statuses(self):
        past_time = timezone.now() - timedelta(minutes=10)
        tids = []
        statuses = [
            Transaction.Status.INVENTORY_TIMEOUT,
            Transaction.Status.PAYMENT_TIMEOUT,
            Transaction.Status.INVENTORY_CONNECTION_ERROR,
            Transaction.Status.PAYMENT_CONNECTION_ERROR,
        ]
        for st in statuses:
            tid = str(uuid.uuid4())
            tids.append(tid)
            Transaction.objects.create(
                transaction_id=tid,
                status=st,
                seat_ids=[1],
                amount=500,
            )
            Transaction.objects.filter(transaction_id=tid).update(updated_at=past_time)

        def mock_get(url, *args, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.json.return_value = {"status": "NOT_CHARGED"}
                return mock
            if "reserve/check" in url:
                mock.status_code = 200
                mock.json.return_value = {"status": Transaction.Status.NOT_RESERVED}
                return mock
            raise ValueError(f"Unexpected GET: {url}")

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=mock_get):
            call_command("recovery_stuck_transactions")

        for tid in tids:
            t = Transaction.objects.get(transaction_id=tid)
            self.assertEqual(t.status, Transaction.Status.TIMEOUT)

    def test_recovery_compensation_when_refund_fails_leaves_compensation_failed(self):
        tid = str(uuid.uuid4())
        Transaction.objects.create(
            transaction_id=tid,
            status=Transaction.Status.PAYMENT_PROCESSING,
            seat_ids=[1],
            amount=500,
        )
        past_time = timezone.now() - timedelta(minutes=10)
        Transaction.objects.filter(transaction_id=tid).update(updated_at=past_time)

        def mock_get(url, *args, **kwargs):
            if "payments/check" in url:
                mock = MagicMock()
                mock.status_code = 200
                mock.json.return_value = {"status": "CHARGED"}
                return mock
            raise ValueError(f"Unexpected GET: {url}")

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "seats/confirm" in url:
                mock.status_code = 409
                mock.json.return_value = {"error": "Hold expired", "code": "HOLD_EXPIRED"}
                return mock
            if "seats/release" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat released"}
                return mock
            if "payments/refund" in url:
                mock.status_code = 500
                mock.json.return_value = {"error": "Gateway timeout"}
                return mock
            raise ValueError(f"Unexpected POST: {url}")

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=mock_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=mock_post):
            call_command("recovery_stuck_transactions")

        txn = Transaction.objects.get(transaction_id=tid)
        self.assertEqual(txn.status, Transaction.Status.COMPENSATION_FAILED)


