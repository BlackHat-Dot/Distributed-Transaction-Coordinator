import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
import requests

from coordinator.models import Transaction


class RetryGatewayTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _mock_response(self, status_code=200, json_data=None):
        r = requests.Response()
        r.status_code = status_code
        r._content = json.dumps(json_data if json_data is not None else {}).encode("utf-8")
        r.headers["Content-Type"] = "application/json"
        return r

    def test_retry_confirm_on_503(self):
        recorded_urls = []
        confirm_call_count = 0

        def fake_post(url, *args, **kwargs):
            nonlocal confirm_call_count
            recorded_urls.append(url)
            if "pricing" in url:
                return self._mock_response(200, [{"id": 1, "price": 500, "row": "A", "seat_number": "1"}])
            if "reserve" in url:
                future_hold = (timezone.now() + timedelta(minutes=10)).isoformat()
                return self._mock_response(200, {
                    "message": "Held",
                    "seat_ids": [1],
                    "held_until": future_hold,
                    "total_amount": "500.00",
                })
            if "charge" in url:
                return self._mock_response(200, {"status": "CHARGED"})
            if "confirm" in url:
                confirm_call_count += 1
                if confirm_call_count == 1:
                    return self._mock_response(503, {"error": "Inventory service unavailable"})
                return self._mock_response(200, {"status": "BOOKED", "seat_ids": [1]})
            if "refund" in url:
                return self._mock_response(200, {"status": "REFUNDED"})
            if "release" in url:
                return self._mock_response(200, {"message": "Released"})
            return self._mock_response(500)

        user_dict = {
            "is_authenticated": True,
            "user_id": "u1",
            "user_email": "u1@dtc.com",
        }

        with patch("coordinator.views.requests.post", side_effect=fake_post), \
             patch("coordinator.views.get_user_from_request", return_value=user_dict):
            # Hold request
            resp_hold = self.client.post(
                "/transactions/?sync=true",
                {"seat_ids": [1], "action": "hold"},
                format="json",
            )
            self.assertEqual(resp_hold.status_code, 200)
            txn_id = resp_hold.data["transaction_id"]

            # Pay request with ?sync=true
            resp_pay = self.client.post(
                "/transactions/?sync=true",
                {"transaction_id": str(txn_id), "action": "pay"},
                format="json",
            )

        # Assertions
        confirm_calls = [u for u in recorded_urls if "confirm" in u]
        self.assertEqual(len(confirm_calls), 2)
        self.assertEqual(resp_pay.status_code, 200)
        txn = Transaction.objects.get(transaction_id=txn_id)
        self.assertEqual(txn.status, Transaction.Status.COMPLETED)
