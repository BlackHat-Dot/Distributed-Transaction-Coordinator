from datetime import timedelta
from decimal import Decimal
import os
import uuid
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from coordinator.auth_views import RequesterIdentity
from coordinator.models import Transaction, SagaEvent, Ticket
from coordinator.throttling import BookingRateThrottle
from coordinator.tickets import issue_tickets_for_transaction


class SagaBookingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user_id = "usr_alice"
        self.user_email = "alice@example.com"
        self.caller_alice = RequesterIdentity(True, self.user_id, False, user_email=self.user_email)
        self.url = "/transactions/"

        # Bypass rate throttling in unit tests
        self.throttle_patch = patch.object(BookingRateThrottle, "allow_request", return_value=True)
        self.throttle_patch.start()
        self.addCleanup(self.throttle_patch.stop)

    def _auth(self, caller=None):
        ident = caller or self.caller_alice
        return patch("coordinator.views._get_caller", return_value=ident)

    def _auth_throttling(self, caller=None):
        ident = caller or self.caller_alice
        return patch("coordinator.throttling._get_caller", return_value=ident)

    def test_unauthenticated_request_rejected(self):
        """Unauthenticated request must return 401/403 with authentication required message."""
        resp = self.client.post(self.url, {"action": "hold", "seat_ids": [1]}, format="json")
        self.assertIn(resp.status_code, (401, 403))

    def test_hold_seats_success(self):
        """Phase 1: Valid seat hold reserves seats and transitions status to HOLDING."""
        future_hold = (timezone.now() + timedelta(minutes=10)).isoformat()

        def fake_http(method, url, **kwargs):
            mock = MagicMock()
            if "seats/pricing" in url:
                mock.status_code = 200
                mock.content = b'[{"id": 1, "price": "100.00", "row": "A", "seat_number": "1"}, {"id": 2, "price": "100.00", "row": "A", "seat_number": "2"}]'
                mock.json.return_value = [
                    {"id": 1, "price": "100.00", "row": "A", "seat_number": "1"},
                    {"id": 2, "price": "100.00", "row": "A", "seat_number": "2"},
                ]
            elif "seats/reserve" in url:
                mock.status_code = 200
                mock.content = b'{"message": "Held"}'
                mock.json.return_value = {
                    "message": "Held",
                    "held_until": future_hold,
                    "event": {"name": "DTC Live Concert", "venue": "Main Arena"},
                }
            return mock

        with self._auth(), self._auth_throttling(), \
             patch("coordinator.views._http", side_effect=fake_http):
            resp = self.client.post(self.url, {"action": "hold", "seat_ids": [1, 2]}, format="json")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data.get("status"), Transaction.Status.HOLDING)
            self.assertEqual(resp.data.get("quantity"), 2)

        tx_id = resp.data.get("transaction_id")
        txn = Transaction.objects.get(transaction_id=tx_id)
        self.assertEqual(txn.status, Transaction.Status.HOLDING)
        self.assertEqual(txn.user_id, self.user_id)
        self.assertEqual(txn.event_name, "DTC Live Concert")

    def test_hold_seats_conflict_when_already_held(self):
        """If inventory reserve returns 409 (e.g. seats already held), status becomes INVENTORY_RESERVE_FAILED."""
        def fake_http(method, url, **kwargs):
            mock = MagicMock()
            if "seats/pricing" in url:
                mock.status_code = 200
                mock.content = b'[{"id": 1, "price": "100.00", "row": "A", "seat_number": "1"}]'
                mock.json.return_value = [{"id": 1, "price": "100.00", "row": "A", "seat_number": "1"}]
            elif "seats/reserve" in url:
                mock.status_code = 409
                mock.content = b'{"error": "Seats already held"}'
                mock.json.return_value = {"error": "Seats already held"}
            elif "seats/release" in url:
                mock.status_code = 200
                mock.content = b'{}'
                mock.json.return_value = {}
            return mock

        with self._auth(), self._auth_throttling(), \
             patch("coordinator.views._http", side_effect=fake_http):
            resp = self.client.post(self.url, {"action": "hold", "seat_ids": [1]}, format="json")
            self.assertEqual(resp.status_code, 409)

        txn = Transaction.objects.first()
        self.assertIsNotNone(txn)
        self.assertEqual(txn.status, Transaction.Status.INVENTORY_RESERVE_FAILED)

    def test_pay_hold_ownership_check(self):
        """Attempting to pay for another user's transaction returns 403 FORBIDDEN."""
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.HOLDING,
            user_id="usr_bob",
            amount=100,
            seat_ids=[1],
        )

        with self._auth(), self._auth_throttling():
            resp = self.client.post(self.url, {"action": "pay", "transaction_id": str(tx_id)}, format="json")
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.data.get("code"), "FORBIDDEN")

    def test_pay_hold_expired_returns_409(self):
        """Payment attempt on an expired hold returns 409 HOLD_EXPIRED and marks status TIMEOUT."""
        tx_id = uuid.uuid4()
        past_hold = (timezone.now() - timedelta(minutes=5)).isoformat()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.HOLDING,
            user_id=self.user_id,
            amount=100,
            seat_ids=[1],
        )
        SagaEvent.objects.create(
            transaction=txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.HOLDING,
            http_payload={"held_until": past_hold},
        )

        with self._auth(), self._auth_throttling(), \
             patch("coordinator.views._release_inventory") as mock_rel:
            resp = self.client.post(self.url, {"action": "pay", "transaction_id": str(tx_id)}, format="json")
            self.assertEqual(resp.status_code, 409)
            self.assertEqual(resp.data.get("code"), "HOLD_EXPIRED")
            mock_rel.assert_called_once()

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.TIMEOUT)

    def test_pay_hold_happy_path_completes_transaction(self):
        """Sync payment on valid hold completes transaction and enables ticket issuance."""
        tx_id = uuid.uuid4()
        future_hold = (timezone.now() + timedelta(minutes=10)).isoformat()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.HOLDING,
            user_id=self.user_id,
            user_email=self.user_email,
            amount=Decimal("150.00"),
            seat_ids=[10],
            seat_numbers=["A10"],
            event_name="Opera Night",
        )
        SagaEvent.objects.create(
            transaction=txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.HOLDING,
            http_payload={"held_until": future_hold},
        )

        def fake_http(method, url, **kwargs):
            mock = MagicMock()
            if "payments/charge" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            elif "seats/confirm" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CONFIRMED"}'
                mock.json.return_value = {"status": "CONFIRMED"}
            return mock

        with self._auth(), self._auth_throttling(), \
             patch("coordinator.views._http", side_effect=fake_http):
            resp = self.client.post(
                f"{self.url}?sync=true",
                {"action": "pay", "transaction_id": str(tx_id)},
                format="json",
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data.get("status"), Transaction.Status.COMPLETED)

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPLETED)

        # Issue and verify tickets for completed transaction
        mock_gt = MagicMock()
        mock_gt.status_code = 200
        mock_gt.json.return_value = {
            "status": "BOOKED",
            "transaction_id": str(txn.transaction_id),
            "seats": [{"seat_id": 10, "seat_label": "A10", "amount": "150.00"}],
        }
        with patch("coordinator.tickets.requests.get", return_value=mock_gt):
            tickets = issue_tickets_for_transaction(txn)
            self.assertEqual(len(tickets), 1)
            self.assertEqual(tickets[0].seat_id, 10)
            self.assertEqual(tickets[0].event_name, "Opera Night")

    def test_pay_hold_payment_declined_compensates(self):
        """Declined payment triggers compensation (releases inventory, marks COMPENSATED)."""
        tx_id = uuid.uuid4()
        future_hold = (timezone.now() + timedelta(minutes=10)).isoformat()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.HOLDING,
            user_id=self.user_id,
            user_email=self.user_email,
            amount=Decimal("100.00"),
            seat_ids=[5],
        )
        SagaEvent.objects.create(
            transaction=txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.HOLDING,
            http_payload={"held_until": future_hold},
        )

        def fake_http(method, url, **kwargs):
            mock = MagicMock()
            if "payments/charge" in url:
                mock.status_code = 402
                mock.content = b'{"status": "DECLINED", "error": "Insufficient funds"}'
                mock.json.return_value = {"status": "DECLINED", "error": "Insufficient funds"}
            elif "payments/void" in url:
                mock.status_code = 200
                mock.content = b'{"status": "VOIDED"}'
                mock.json.return_value = {"status": "VOIDED"}
            elif "seats/release" in url:
                mock.status_code = 200
                mock.content = b'{"message": "Released"}'
                mock.json.return_value = {"message": "Released"}
            return mock

        with self._auth(), self._auth_throttling(), \
             patch("coordinator.views._http", side_effect=fake_http):
            resp = self.client.post(
                f"{self.url}?sync=true",
                {"action": "pay", "transaction_id": str(tx_id)},
                format="json",
            )
            self.assertIn(resp.status_code, (200, 400, 402))

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED)

    def test_pay_hold_confirm_failure_triggers_refund_and_release(self):
        """If confirm fails after payment is charged, automatic refund and inventory release are issued."""
        tx_id = uuid.uuid4()
        future_hold = (timezone.now() + timedelta(minutes=10)).isoformat()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.HOLDING,
            user_id=self.user_id,
            user_email=self.user_email,
            amount=Decimal("100.00"),
            seat_ids=[5],
        )
        SagaEvent.objects.create(
            transaction=txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.HOLDING,
            http_payload={"held_until": future_hold},
        )

        calls = []

        def fake_http(method, url, **kwargs):
            calls.append(url)
            mock = MagicMock()
            if "payments/charge" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            elif "seats/confirm" in url:
                mock.status_code = 409
                mock.content = b'{"error": "Hold expired during payment"}'
                mock.json.return_value = {"error": "Hold expired during payment"}
            elif "payments/refund" in url:
                mock.status_code = 200
                mock.content = b'{"status": "REFUNDED"}'
                mock.json.return_value = {"status": "REFUNDED"}
            elif "seats/release" in url:
                mock.status_code = 200
                mock.content = b'{"message": "Released"}'
                mock.json.return_value = {"message": "Released"}
            return mock

        with self._auth(), self._auth_throttling(), \
             patch("coordinator.views._http", side_effect=fake_http):
            resp = self.client.post(
                f"{self.url}?sync=true",
                {"action": "pay", "transaction_id": str(tx_id)},
                format="json",
            )
            self.assertIn(resp.status_code, (200, 409))

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED)
        self.assertTrue(any("payments/refund" in u for u in calls))
        self.assertTrue(any("seats/release" in u for u in calls))

    def test_pay_hold_confirm_gateway_timeout_leaves_in_recovery(self):
        """Gateway 503 during confirm leaves transaction in PAYMENT_PROCESSING for sweeper recovery."""
        tx_id = uuid.uuid4()
        future_hold = (timezone.now() + timedelta(minutes=10)).isoformat()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.HOLDING,
            user_id=self.user_id,
            user_email=self.user_email,
            amount=Decimal("100.00"),
            seat_ids=[5],
        )
        SagaEvent.objects.create(
            transaction=txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.HOLDING,
            http_payload={"held_until": future_hold},
        )

        def fake_http(method, url, **kwargs):
            mock = MagicMock()
            if "payments/charge" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            elif "seats/confirm" in url:
                mock.status_code = 503
                mock.content = b'{"error": "Gateway Timeout"}'
                mock.json.return_value = {"error": "Gateway Timeout"}
            return mock

        with self._auth(), self._auth_throttling(), \
             patch("coordinator.views._http", side_effect=fake_http) as mock_http:
            resp = self.client.post(
                f"{self.url}?sync=true",
                {"action": "pay", "transaction_id": str(tx_id)},
                format="json",
            )
            self.assertEqual(resp.status_code, 503)
            self.assertTrue(resp.data.get("needs_recovery"))

            # Refund must NEVER be called while confirm is uncertain
            call_urls = [str(arg) for call in mock_http.call_args_list for arg in call.args if isinstance(arg, str)]
            self.assertFalse(any("payments/refund" in u for u in call_urls))

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.PAYMENT_PROCESSING)

    def test_pay_already_completed_is_idempotent(self):
        """Paying an already COMPLETED transaction short-circuits to 200 without re-charging."""
        tx_id = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.COMPLETED,
            user_id=self.user_id,
            user_email=self.user_email,
            amount=Decimal("100.00"),
            seat_ids=[5],
        )

        with self._auth(), self._auth_throttling(), \
             patch("coordinator.views._http") as mock_http:
            resp = self.client.post(
                f"{self.url}?sync=true",
                {"action": "pay", "transaction_id": str(tx_id)},
                format="json",
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data.get("status"), Transaction.Status.COMPLETED)
            mock_http.assert_not_called()
