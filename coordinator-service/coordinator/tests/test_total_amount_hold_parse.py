from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from coordinator.models import Transaction


class TotalAmountHoldParseTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _mock_response(self, status_code=200, json_data=None):
        mock = MagicMock()
        mock.status_code = status_code
        mock.json.return_value = json_data or {}
        return mock

    def test_hold_handles_malformed_total_amount_without_crash(self):
        """Invalid total_amount string from inventory must not crash with unhandled InvalidOperation."""
        def fake_post(url, *args, **kwargs):
            if "pricing" in url:
                return self._mock_response(200, [{"id": 1, "price": 500, "row": "A", "seat_number": "1"}])
            if "reserve" in url:
                future_hold = (timezone.now() + timedelta(minutes=10)).isoformat()
                return self._mock_response(200, {
                    "message": "Held",
                    "seat_ids": [1],
                    "held_until": future_hold,
                    "total_amount": "not_a_valid_number",
                })
            return self._mock_response(500)

        with patch("coordinator.views.requests.post", side_effect=fake_post), \
             patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "u1", "user_email": "u1@dtc.com"}):
            resp = self.client.post("/transactions/?sync=true", {"seat_ids": [1], "action": "hold"}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Transaction.Status.HOLDING)
        txn = Transaction.objects.first()
        self.assertEqual(txn.status, Transaction.Status.HOLDING)
        self.assertEqual(txn.amount, 500)
