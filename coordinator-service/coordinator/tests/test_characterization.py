import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent


class CharacterizationTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _mock_response(self, status_code=200, json_data=None):
        mock = MagicMock()
        mock.status_code = status_code
        mock.json.return_value = json_data or {}
        return mock

    def test_1_hold_success(self):
        """1. Hold success: seats held, returns 200 and HOLDING status."""
        def fake_post(url, *args, **kwargs):
            if "pricing" in url:
                return self._mock_response(200, [{"id": 1, "price": 500, "row": "A", "seat_number": "1"}])
            if "reserve" in url:
                future_hold = (timezone.now() + timedelta(minutes=10)).isoformat()
                return self._mock_response(200, {"message": "Held", "seat_ids": [1], "held_until": future_hold})
            return self._mock_response(500)

        with patch("coordinator.views.requests.post", side_effect=fake_post), \
             patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "u1", "user_email": "u1@dtc.com"}):
            resp = self.client.post("/transactions/?sync=true", {"seat_ids": [1], "action": "hold"}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Transaction.Status.HOLDING)
        self.assertIn("held_until", resp.data)
        self.assertEqual(Transaction.objects.filter(status=Transaction.Status.HOLDING).count(), 1)

    def test_2_hold_with_reserve_failure(self):
        """2. Hold with reserve failure: inventory returns 409, coordinator releases and fails."""
        def fake_post(url, *args, **kwargs):
            if "pricing" in url:
                return self._mock_response(200, [{"id": 1, "price": 500, "row": "A", "seat_number": "1"}])
            if "reserve" in url:
                return self._mock_response(409, {"error": "Seat already reserved", "code": "SEAT_NOT_AVAILABLE"})
            if "release" in url:
                return self._mock_response(200, {"message": "Released"})
            return self._mock_response(500)

        with patch("coordinator.views.requests.post", side_effect=fake_post), \
             patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "u1", "user_email": "u1@dtc.com"}):
            resp = self.client.post("/transactions/?sync=true", {"seat_ids": [1], "action": "hold"}, format="json")

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "SEAT_NOT_AVAILABLE")
        txn = Transaction.objects.first()
        self.assertEqual(txn.status, Transaction.Status.INVENTORY_RESERVE_FAILED)

    def test_3_pay_success(self):
        """3. Pay success: transitions HOLDING to COMPLETED."""
        txn_id = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=txn_id,
            status=Transaction.Status.HOLDING,
            seat_ids=[1],
            seat_numbers="A1",
            amount=Decimal("500.00"),
            quantity=1,
            user_id="u1",
            user_email="u1@dtc.com",
        )
        SagaEvent.objects.create(
            transaction=txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.HOLDING,
            http_payload={"held_until": (timezone.now() + timedelta(minutes=5)).isoformat()},
        )

        def fake_post(url, *args, **kwargs):
            if "charge" in url:
                return self._mock_response(200, {"status": "CHARGED"})
            if "confirm" in url:
                return self._mock_response(200, {"status": "BOOKED", "seat_ids": [1]})
            return self._mock_response(500)

        with patch("coordinator.views.requests.post", side_effect=fake_post), \
             patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "u1", "user_email": "u1@dtc.com"}):
            resp = self.client.post("/transactions/?sync=true", {"transaction_id": str(txn_id), "action": "pay"}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Transaction.Status.COMPLETED)
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPLETED)

    def test_4_pay_with_payment_decline(self):
        """4. Pay with payment decline: triggers compensation path."""
        txn_id = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=txn_id,
            status=Transaction.Status.HOLDING,
            seat_ids=[1],
            seat_numbers="A1",
            amount=Decimal("500.00"),
            quantity=1,
            user_id="u1",
            user_email="u1@dtc.com",
        )
        SagaEvent.objects.create(
            transaction=txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.HOLDING,
            http_payload={"held_until": (timezone.now() + timedelta(minutes=5)).isoformat()},
        )

        def fake_post(url, *args, **kwargs):
            if "charge" in url:
                return self._mock_response(400, {"status": "FAILED", "error": "Insufficient funds"})
            if "release" in url:
                return self._mock_response(200, {"message": "Released"})
            if "void" in url:
                return self._mock_response(200, {"status": "VOIDED"})
            return self._mock_response(500)

        with patch("coordinator.views.requests.post", side_effect=fake_post), \
             patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "u1", "user_email": "u1@dtc.com"}):
            resp = self.client.post("/transactions/?sync=true", {"transaction_id": str(txn_id), "action": "pay"}, format="json")

        self.assertEqual(resp.status_code, 400)
        txn.refresh_from_db()
        self.assertIn(txn.status, [Transaction.Status.COMPENSATED, Transaction.Status.PAYMENT_FAILED])

    def test_5_pay_on_expired_hold(self):
        """5. Pay on expired hold: returns 409 HOLD_EXPIRED and sets TIMEOUT."""
        txn_id = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=txn_id,
            status=Transaction.Status.HOLDING,
            seat_ids=[1],
            seat_numbers="A1",
            amount=Decimal("500.00"),
            quantity=1,
            user_id="u1",
            user_email="u1@dtc.com",
        )
        SagaEvent.objects.create(
            transaction=txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.HOLDING,
            http_payload={"held_until": (timezone.now() - timedelta(minutes=5)).isoformat()},
        )

        with patch("coordinator.views.requests.post", return_value=self._mock_response(200)), \
             patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "u1", "user_email": "u1@dtc.com"}):
            resp = self.client.post("/transactions/?sync=true", {"transaction_id": str(txn_id), "action": "pay"}, format="json")

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "HOLD_EXPIRED")
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.TIMEOUT)

    def test_6_retry_on_non_eligible_status(self):
        """6. Retry compensation on non-eligible status (e.g. STARTED) returns 400."""
        txn_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=txn_id,
            status=Transaction.Status.STARTED,
            seat_ids=[1],
            amount=Decimal("100.00"),
            quantity=1,
            user_id="u1",
        )
        from coordinator.views import RequesterIdentity
        with patch("coordinator.views._get_requester_identity", return_value=RequesterIdentity(True, "staff1", True, "admin@dtc.internal")):
            resp = self.client.post(f"/transactions/{txn_id}/retry-compensation/", format="json")

        self.assertEqual(resp.status_code, 400)

    def test_7_idor_non_staff_denied_on_other_users_transaction(self):
        """7. IDOR rule: non-staff user gets 403 on someone else's transaction."""
        txn_id = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=txn_id,
            status=Transaction.Status.HOLDING,
            seat_ids=[1],
            seat_numbers="A1",
            amount=Decimal("500.00"),
            quantity=1,
            user_id="victim_user",
            user_email="victim@dtc.com",
        )

        attacker_auth = {"is_authenticated": True, "user_id": "attacker_user", "user_email": "attacker@other.com"}

        # Attempt to GET victim's transaction
        with patch("coordinator.views.get_user_from_request", return_value=attacker_auth):
            resp_get = self.client.get(f"/transactions/{txn_id}/")
        self.assertEqual(resp_get.status_code, 403)

        # Attempt to PAY victim's transaction
        with patch("coordinator.views.get_user_from_request", return_value=attacker_auth):
            resp_pay = self.client.post("/transactions/?sync=true", {"transaction_id": str(txn_id), "action": "pay"}, format="json")
        self.assertEqual(resp_pay.status_code, 403)
        self.assertEqual(resp_pay.data.get("code"), "FORBIDDEN")
