import json
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch
from django.test import TestCase
from coordinator.models import Transaction
from coordinator.views import _charge


class TestF1CheckoutPresence(TestCase):
    def test_checkout_presence_set_during_payment_and_cleared_on_success(self):
        """
        F1: Coordinator must set Redis key dtc:checkout:{transaction_id}
        when saga enters payment step, and clear it on success.
        """
        tid = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=tid,
            status=Transaction.Status.PAYMENT_PROCESSING,
            amount=Decimal("150.00"),
            seat_ids=[1, 2],
        )

        mock_redis = MagicMock()
        presence_seen_during_payment = []

        def mock_charge_post(url, **kwargs):
            if "payments/charge/" in url:
                key = f"dtc:checkout:{tid}"
                set_calls = [c for c in mock_redis.set.call_args_list if c.args and c.args[0] == key]
                presence_seen_during_payment.append(len(set_calls) > 0)
                resp = MagicMock()
                resp.status_code = 200
                resp.content = json.dumps({"status": "CHARGED", "transaction_id": str(tid)}).encode("utf-8")
                resp.json.return_value = {"status": "CHARGED", "transaction_id": str(tid)}
                return resp
            if "inventory/seats/confirm/" in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.content = json.dumps({"status": "BOOKED"}).encode("utf-8")
                resp.json.return_value = {"status": "BOOKED"}
                return resp
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b"{}"
            resp.json.return_value = {}
            return resp

        with patch("coordinator.views._get_coordinator_redis_client", return_value=mock_redis):
            with patch("coordinator.views._http_post", side_effect=mock_charge_post):
                resp = _charge(
                    tid,
                    amount=Decimal("150.00"),
                    fail_compensation=False,
                    seat_ids=[1, 2],
                )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(presence_seen_during_payment and presence_seen_during_payment[0],
                        "dtc:checkout:{tid} must be set in Redis during payment step")
        # Cleared on success
        key = f"dtc:checkout:{tid}"
        delete_calls = [c for c in mock_redis.delete.call_args_list if c.args and c.args[0] == key]
        self.assertTrue(len(delete_calls) > 0, "dtc:checkout:{tid} must be deleted on success")

    def test_checkout_presence_cleared_on_compensation_failure(self):
        """
        F1: Coordinator must clear dtc:checkout:{transaction_id}
        when payment fails and compensation completes.
        """
        tid = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=tid,
            status=Transaction.Status.PAYMENT_PROCESSING,
            amount=Decimal("150.00"),
            seat_ids=[1, 2],
        )

        mock_redis = MagicMock()

        def mock_charge_post(url, **kwargs):
            if "payments/charge/" in url:
                resp = MagicMock()
                resp.status_code = 400
                resp.content = json.dumps({"error": "Declined", "status": "DECLINED"}).encode("utf-8")
                resp.json.return_value = {"error": "Declined", "status": "DECLINED"}
                return resp
            if "payments/void/" in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.content = json.dumps({"status": "VOIDED"}).encode("utf-8")
                resp.json.return_value = {"status": "VOIDED"}
                return resp
            if "inventory/seats/release/" in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.content = json.dumps({"status": "RELEASED"}).encode("utf-8")
                resp.json.return_value = {"status": "RELEASED"}
                return resp
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b"{}"
            resp.json.return_value = {}
            return resp

        with patch("coordinator.views._get_coordinator_redis_client", return_value=mock_redis):
            with patch("coordinator.views._http_post", side_effect=mock_charge_post):
                resp = _charge(
                    tid,
                    amount=Decimal("150.00"),
                    fail_compensation=False,
                    seat_ids=[1, 2],
                )

        key = f"dtc:checkout:{tid}"
        delete_calls = [c for c in mock_redis.delete.call_args_list if c.args and c.args[0] == key]
        self.assertTrue(len(delete_calls) > 0, "dtc:checkout:{tid} must be deleted on compensation")
