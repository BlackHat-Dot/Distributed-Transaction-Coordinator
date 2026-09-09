import uuid
from unittest.mock import MagicMock, patch
from datetime import timedelta

from django.core.management import call_command
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent


class ForwardSagaEventTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()

    def test_happy_path_produces_ordered_saga_events(self):
        tid = str(uuid.uuid4())

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "reserve" in url:
                mock.status_code = 200
                mock.json.return_value = {
                    "message": "Seat reserved",
                    "seat_ids": [1],
                    "status": "INVENTORY_RESERVED",
                }
                return mock
            if "charge" in url:
                mock.status_code = 200
                mock.json.return_value = {
                    "transaction_id": tid,
                    "amount": "500.00",
                    "status": "CHARGED",
                }
                return mock
            if "confirm" in url:
                mock.status_code = 200
                mock.json.return_value = {
                    "message": "Seat booked successfully",
                    "seat_ids": [1],
                    "status": "BOOKED",
                }
                return mock
            raise ValueError(f"Unexpected POST url: {url}")

        with patch("coordinator.views.requests.post", side_effect=mock_post):
            resp = self.client.post("/transactions/?sync=true",
                {"transaction_id": tid, "seat_ids": [1], "quantity": 1, "amount": 500},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Transaction.Status.COMPLETED)

        events_resp = self.client.get(f"/transactions/{tid}/events/")
        self.assertEqual(events_resp.status_code, 200)
        self.assertEqual(events_resp.data["status"], Transaction.Status.COMPLETED)

        events = events_resp.data["events"]
        self.assertEqual(len(events), 3, f"Expected 3 forward events, got: {events}")

        self.assertEqual(events[0]["seq"], 1)
        self.assertEqual(events[0]["from_status"], Transaction.Status.STARTED)
        self.assertEqual(events[0]["to_status"], Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[0]["actor"], SagaEvent.ACTOR_REQUEST)
        self.assertEqual(events[0]["http_status_code"], 200)

        self.assertEqual(events[1]["seq"], 2)
        self.assertEqual(events[1]["from_status"], Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[1]["to_status"], Transaction.Status.PAYMENT_PROCESSING)
        self.assertEqual(events[1]["actor"], SagaEvent.ACTOR_REQUEST)

        self.assertEqual(events[2]["seq"], 3)
        self.assertEqual(events[2]["from_status"], Transaction.Status.PAYMENT_PROCESSING)
        self.assertEqual(events[2]["to_status"], Transaction.Status.COMPLETED)
        self.assertEqual(events[2]["actor"], SagaEvent.ACTOR_REQUEST)
        self.assertEqual(events[2]["http_status_code"], 200)

    def test_payment_decline_forward_compensation_produces_complete_event_trail(self):
        tid = str(uuid.uuid4())

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "reserve" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat reserved", "seat_ids": [1]}
                return mock
            if "charge" in url:
                mock.status_code = 422
                mock.json.return_value = {
                    "error": "Payment declined",
                    "transaction_id": tid,
                    "status": "NOT_CHARGED",
                }
                return mock
            if "release" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat released", "transaction_id": tid}
                return mock
            raise ValueError(f"Unexpected POST url: {url}")

        with patch("coordinator.views.requests.post", side_effect=mock_post):
            resp = self.client.post("/transactions/?sync=true",
                {"transaction_id": tid, "seat_ids": [1], "amount": 9999},
                format="json",
            )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["status"], Transaction.Status.COMPENSATED)

        events_resp = self.client.get(f"/transactions/{tid}/events/")
        self.assertEqual(events_resp.status_code, 200)
        events = events_resp.data["events"]
        self.assertEqual(len(events), 4, f"Expected 4 events on compensation path, got: {events}")

        self.assertEqual(events[0]["seq"], 1)
        self.assertEqual(events[0]["from_status"], Transaction.Status.STARTED)
        self.assertEqual(events[0]["to_status"], Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[0]["actor"], SagaEvent.ACTOR_REQUEST)

        self.assertEqual(events[1]["seq"], 2)
        self.assertEqual(events[1]["from_status"], Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[1]["to_status"], Transaction.Status.PAYMENT_PROCESSING)
        self.assertEqual(events[1]["actor"], SagaEvent.ACTOR_REQUEST)

        self.assertEqual(events[2]["seq"], 3)
        self.assertEqual(events[2]["from_status"], Transaction.Status.PAYMENT_PROCESSING)
        self.assertEqual(events[2]["to_status"], Transaction.Status.COMPENSATING)
        self.assertEqual(events[2]["actor"], SagaEvent.ACTOR_REQUEST)
        self.assertEqual(events[2]["http_status_code"], 422)

        self.assertEqual(events[3]["seq"], 4)
        self.assertEqual(events[3]["from_status"], Transaction.Status.COMPENSATING)
        self.assertEqual(events[3]["to_status"], Transaction.Status.COMPENSATED)
        self.assertEqual(events[3]["actor"], SagaEvent.ACTOR_REQUEST)
        self.assertEqual(events[3]["http_status_code"], 200)

    def test_payment_succeeded_but_seat_hold_expired_triggers_compensation(self):
        tid = str(uuid.uuid4())

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "reserve" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat reserved", "seat_ids": [1]}
                return mock
            if "charge" in url:
                mock.status_code = 200
                mock.json.return_value = {"status": "CHARGED", "transaction_id": tid}
                return mock
            if "confirm" in url:
                mock.status_code = 409
                mock.json.return_value = {
                    "error": "Seat hold has expired before confirmation",
                    "status": "HOLD_EXPIRED",
                }
                return mock
            if "release" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat released"}
                return mock
            if "refund" in url:
                mock.status_code = 200
                mock.json.return_value = {"status": "REFUNDED", "transaction_id": tid}
                return mock
            raise ValueError(f"Unexpected POST url: {url}")

        with patch("coordinator.views.requests.post", side_effect=mock_post):
            resp = self.client.post("/transactions/?sync=true",
                {"transaction_id": tid, "seat_ids": [1], "amount": 500},
                format="json",
            )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["status"], Transaction.Status.COMPENSATED)

        txn = Transaction.objects.get(transaction_id=tid)
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED)

    def test_inventory_reserve_failure_produces_event(self):
        tid = str(uuid.uuid4())

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "reserve" in url:
                mock.status_code = 409
                mock.json.return_value = {"error": "Seat is currently held by another user"}
                return mock
            raise ValueError(f"Unexpected POST url: {url}")

        with patch("coordinator.views.requests.post", side_effect=mock_post):
            resp = self.client.post("/transactions/?sync=true",
                {"transaction_id": tid, "seat_ids": [99], "amount": 500},
                format="json",
            )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], Transaction.Status.INVENTORY_RESERVE_FAILED)

        events_resp = self.client.get(f"/transactions/{tid}/events/")
        events = events_resp.data["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["seq"], 1)
        self.assertEqual(events[0]["from_status"], Transaction.Status.STARTED)
        self.assertEqual(events[0]["to_status"], Transaction.Status.INVENTORY_RESERVE_FAILED)
        self.assertEqual(events[0]["actor"], SagaEvent.ACTOR_REQUEST)
        self.assertEqual(events[0]["http_status_code"], 409)

    def test_recovery_sweep_roll_forward_and_roll_back_events(self):
        tid_forward = str(uuid.uuid4())
        txn_f = Transaction.objects.create(
            transaction_id=tid_forward,
            status=Transaction.Status.PAYMENT_PROCESSING,
            seat_ids=[1],
            amount=500,
        )
        past_time = timezone.now() - timedelta(minutes=10)
        Transaction.objects.filter(pk=txn_f.pk).update(updated_at=past_time)

        mock_payment_charged = MagicMock()
        mock_payment_charged.status_code = 200
        mock_payment_charged.json.return_value = {"status": "CHARGED"}

        mock_confirm_ok = MagicMock()
        mock_confirm_ok.status_code = 200
        mock_confirm_ok.json.return_value = {"message": "Seats confirmed"}

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", return_value=mock_payment_charged), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", return_value=mock_confirm_ok):
            call_command("recovery_stuck_transactions")

        events_f = self.client.get(f"/transactions/{tid_forward}/events/").data["events"]
        self.assertEqual(len(events_f), 2)
        self.assertEqual(events_f[0]["to_status"], Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events_f[0]["actor"], SagaEvent.ACTOR_RECOVERY)
        self.assertEqual(events_f[1]["to_status"], Transaction.Status.COMPLETED)
        self.assertEqual(events_f[1]["actor"], SagaEvent.ACTOR_RECOVERY)
        self.assertEqual(events_f[1]["http_status_code"], 200)

        tid_back = str(uuid.uuid4())
        txn_b = Transaction.objects.create(
            transaction_id=tid_back,
            status=Transaction.Status.PAYMENT_PROCESSING,
            seat_ids=[1],
            amount=500,
        )
        Transaction.objects.filter(pk=txn_b.pk).update(updated_at=past_time)

        def mock_recovery_get(url, *args, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.json.return_value = {"status": "NOT_CHARGED"}
                return mock
            if "reserve/check" in url:
                mock.status_code = 200
                mock.json.return_value = {"status": Transaction.Status.INVENTORY_RESERVED}
                return mock
            raise ValueError(f"Unexpected GET: {url}")

        mock_release = MagicMock()
        mock_release.status_code = 200
        mock_release.json.return_value = {"message": "Seat released"}

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=mock_recovery_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", return_value=mock_release):
            call_command("recovery_stuck_transactions")

        events_b = self.client.get(f"/transactions/{tid_back}/events/").data["events"]
        self.assertEqual(len(events_b), 3)
        self.assertEqual(events_b[0]["to_status"], Transaction.Status.RECOVERY_IN_PROGRESS)
        self.assertEqual(events_b[1]["to_status"], Transaction.Status.COMPENSATING)
        self.assertEqual(events_b[2]["to_status"], Transaction.Status.COMPENSATED)
        for ev in events_b:
            self.assertEqual(ev["actor"], SagaEvent.ACTOR_RECOVERY)
