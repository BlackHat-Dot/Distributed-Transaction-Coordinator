import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent
from coordinator.tasks import execute_saga_task


class NonBlockingSagaTaskTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    @patch("coordinator.tasks.execute_saga_task.delay")
    def test_post_transactions_returns_202_accepted_and_dispatches_task(self, mock_delay):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            "/transactions/?async=true",
            {"transaction_id": tid, "seat_id": 1, "quantity": 1, "amount": 500},
            format="json",
        )
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.data["status"], "PENDING")
        self.assertEqual(resp.data["transaction_id"], tid)
        self.assertEqual(resp.get("Location"), f"/transactions/{tid}/")

        # Verify DB transaction was created in STARTED state
        txn = Transaction.objects.get(transaction_id=tid)
        self.assertEqual(txn.status, Transaction.Status.STARTED)

        # Verify task was enqueued
        mock_delay.assert_called_once()

    def test_get_transaction_polling_endpoint(self):
        tid = str(uuid.uuid4())
        txn = Transaction.objects.create(
            transaction_id=tid,
            status=Transaction.Status.STARTED,
            seat_id=1,
            amount=500,
        )

        resp = self.client.get(f"/transactions/{tid}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["transaction_id"], tid)
        self.assertEqual(resp.data["status"], Transaction.Status.STARTED)
        self.assertFalse(resp.data["is_terminal"])

        # Update to terminal status
        txn.status = Transaction.Status.COMPLETED
        txn.save()

        resp2 = self.client.get(f"/transactions/{tid}/")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data["status"], Transaction.Status.COMPLETED)
        self.assertTrue(resp2.data["is_terminal"])

    def test_execute_saga_task_happy_path_completes_saga(self):
        tid = str(uuid.uuid4())
        Transaction.objects.create(
            transaction_id=tid,
            status=Transaction.Status.STARTED,
            seat_id=1,
            amount=500,
        )

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "reserve" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat reserved", "seat_id": 1}
                return mock
            if "charge" in url:
                mock.status_code = 200
                mock.json.return_value = {"transaction_id": tid, "amount": "500.00", "status": "CHARGED"}
                return mock
            if "confirm" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat booked successfully", "seat_id": 1, "status": "BOOKED"}
                return mock
            raise ValueError(f"Unexpected POST: {url}")

        with patch("coordinator.views.requests.post", side_effect=mock_post):
            result = execute_saga_task(tid, seat_id=1, amount=500)
            self.assertEqual(result, "completed")

        txn = Transaction.objects.get(transaction_id=tid)
        self.assertEqual(txn.status, Transaction.Status.COMPLETED)

        # Inspect SagaEvents written by async task
        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].from_status, "STARTED")
        self.assertEqual(events[0].to_status, "INVENTORY_RESERVED")
        self.assertEqual(events[1].from_status, "INVENTORY_RESERVED")
        self.assertEqual(events[1].to_status, "PAYMENT_PROCESSING")
        self.assertEqual(events[2].from_status, "PAYMENT_PROCESSING")
        self.assertEqual(events[2].to_status, "COMPLETED")

    def test_sync_param_override_returns_synchronous_response(self):
        tid = str(uuid.uuid4())

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "reserve" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat reserved", "seat_id": 1}
                return mock
            if "charge" in url:
                mock.status_code = 200
                mock.json.return_value = {"transaction_id": tid, "amount": "500.00", "status": "CHARGED"}
                return mock
            if "confirm" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat booked successfully", "seat_id": 1, "status": "BOOKED"}
                return mock
            raise ValueError(f"Unexpected POST: {url}")

        with patch("coordinator.views.requests.post", side_effect=mock_post):
            resp = self.client.post(
                "/transactions/?sync=true",
                {"transaction_id": tid, "seat_id": 1, "quantity": 1, "amount": 500},
                format="json",
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data["status"], Transaction.Status.COMPLETED)
