from datetime import timedelta
import uuid
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent


CREATE_URL = "/transactions/"
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
                mock.json.return_value = {"message": "Seat reserved", "seat_id": 1}
                return mock
            if "charge" in url:
                raise SimulatedProcessDeath("Coordinator process killed mid-charge")
            raise ValueError(f"Unexpected POST url: {url}")

        with patch("coordinator.views.requests.post", side_effect=mock_post_first_life):
            with self.assertRaises(SimulatedProcessDeath):
                self.client.post(
                    CREATE_URL,
                    {"transaction_id": tid, "seat_id": 1, "quantity": 1, "amount": 500},
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
            raise ValueError(f"Unexpected GET url: {url}")

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=mock_get_recovery):
            call_command("recovery_stuck_transactions")


        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPLETED)

        
        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertEqual(len(events), 4)
        
        # Pre-crash forward events from first life
        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[0].from_status, Transaction.Status.STARTED)
        self.assertEqual(events[0].to_status, Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[0].actor, SagaEvent.ACTOR_REQUEST)

        self.assertEqual(events[1].seq, 2)
        self.assertEqual(events[1].from_status, Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[1].to_status, Transaction.Status.PAYMENT_PROCESSING)
        self.assertEqual(events[1].actor, SagaEvent.ACTOR_REQUEST)

        # Post-crash recovery events
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
                mock.json.return_value = {"message": "Seat reserved", "seat_id": 1}
                return mock
            if "charge" in url:
                raise SimulatedProcessDeath("Coordinator process killed before charge")
            raise ValueError(f"Unexpected POST url: {url}")

       
        with patch("coordinator.views.requests.post", side_effect=mock_post_first_life):
            with self.assertRaises(SimulatedProcessDeath):
                self.client.post(
                    CREATE_URL,
                    {"transaction_id": tid, "seat_id": 1, "quantity": 1, "amount": 500},
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

        # Pre-crash forward events
        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[0].from_status, Transaction.Status.STARTED)
        self.assertEqual(events[0].to_status, Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[0].actor, SagaEvent.ACTOR_REQUEST)

        self.assertEqual(events[1].seq, 2)
        self.assertEqual(events[1].from_status, Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[1].to_status, Transaction.Status.PAYMENT_PROCESSING)
        self.assertEqual(events[1].actor, SagaEvent.ACTOR_REQUEST)

        # Post-crash recovery events
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
