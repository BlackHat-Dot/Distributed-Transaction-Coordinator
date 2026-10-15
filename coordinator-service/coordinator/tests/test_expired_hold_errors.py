import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent


class ExpiredHoldErrorsTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _mock_response(self, status_code=200, json_data=None):
        mock = MagicMock()
        mock.status_code = status_code
        mock.json.return_value = json_data or {}
        return mock

    def test_expired_hold_error_does_not_reach_charge(self):
        """When an expired hold transitions to TIMEOUT and raises an error, charge must not be called."""
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

        charge_calls = []

        def fake_post(url, *args, **kwargs):
            if "charge" in url:
                charge_calls.append(url)
                return self._mock_response(200, {"status": "CHARGED"})
            if "confirm" in url:
                return self._mock_response(200, {"status": "BOOKED", "seat_ids": [1]})
            if "release" in url:
                return self._mock_response(200, {"message": "Released"})
            return self._mock_response(200)

        def fake_set_status_with_event(tx_id, new_status, *args, **kwargs):
            if new_status == Transaction.Status.TIMEOUT:
                raise RuntimeError("Database error during TIMEOUT transition")
            from coordinator.saga_log import _set_status_with_event as real_set_status
            return real_set_status(tx_id, new_status, *args, **kwargs)

        with patch("coordinator.views.requests.post", side_effect=fake_post), \
             patch("coordinator.views._set_status_with_event", side_effect=fake_set_status_with_event), \
             patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "u1", "user_email": "u1@dtc.com"}):
            resp = self.client.post("/transactions/?sync=true", {"transaction_id": str(txn_id), "action": "pay"}, format="json")

        self.assertEqual(len(charge_calls), 0, "Pay request must never reach payment charge URL on expired hold")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "HOLD_EXPIRED")
