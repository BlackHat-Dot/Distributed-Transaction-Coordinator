import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase

from coordinator.models import Transaction
from coordinator.views import _charge


class Payment5xxA5Tests(TestCase):
    def setUp(self):
        self.tx_id = uuid.uuid4()

    def test_charge_5xx_routes_through_payment_check_and_refunds_if_charged(self):
        """F-05: 5xx from /payments/charge/ must query PAYMENT_CHECK; if charged, must refund."""
        _txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.PAYMENT_PROCESSING,
            seat_ids=[1],
            quantity=1,
            amount=Decimal("100"),
        )

        mock_charge_resp = MagicMock()
        mock_charge_resp.status_code = 500
        mock_charge_resp.content = b'{"error": "Internal gateway failure"}'
        mock_charge_resp.json.return_value = {"error": "Internal gateway failure"}

        mock_check_resp = MagicMock()
        mock_check_resp.status_code = 200
        mock_check_resp.content = b'{"status": "CHARGED"}'
        mock_check_resp.json.return_value = {"status": "CHARGED"}

        mock_refund_resp = MagicMock()
        mock_refund_resp.status_code = 200
        mock_refund_resp.content = b'{"status": "REFUNDED"}'
        mock_refund_resp.json.return_value = {"status": "REFUNDED"}

        mock_release_resp = MagicMock()
        mock_release_resp.status_code = 200
        mock_release_resp.content = b'{"status": "RELEASED"}'
        mock_release_resp.json.return_value = {"status": "RELEASED"}

        mock_confirm_resp = MagicMock()
        mock_confirm_resp.status_code = 409
        mock_confirm_resp.content = b'{"error": "Hold expired"}'
        mock_confirm_resp.json.return_value = {"error": "Hold expired"}

        def mock_post(url, *args, **kwargs):
            if "charge" in url:
                return mock_charge_resp
            if "confirm" in url:
                return mock_confirm_resp
            if "refund" in url:
                return mock_refund_resp
            if "release" in url:
                return mock_release_resp
            raise ValueError(f"Unexpected POST url: {url}")

        with patch("coordinator.views._http_post", side_effect=mock_post) as p_post, \
             patch("coordinator.views._http_get", return_value=mock_check_resp) as p_get:
            _charge(
                transaction_id=self.tx_id,
                amount=Decimal("100"),
                seat_ids=[1],
            )
            # Must query PAYMENT_CHECK
            p_get.assert_called_once()
            # And because check returned CHARGED, must call refund
            refund_called = any("refund" in call.args[0] for call in p_post.call_args_list)
            self.assertTrue(refund_called, "Payment refund was not called despite payment being CHARGED downstream!")

    def test_charge_422_does_not_call_payment_check(self):
        """F-05: 422 from /payments/charge/ (declined) must NOT query PAYMENT_CHECK."""
        _txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.PAYMENT_PROCESSING,
            seat_ids=[1],
            quantity=1,
            amount=Decimal("100"),
        )

        mock_charge_resp = MagicMock()
        mock_charge_resp.status_code = 422
        mock_charge_resp.content = b'{"error": "Payment declined", "status": "NOT_CHARGED"}'
        mock_charge_resp.json.return_value = {"error": "Payment declined", "status": "NOT_CHARGED"}

        mock_release_resp = MagicMock()
        mock_release_resp.status_code = 200
        mock_release_resp.content = b'{"status": "RELEASED"}'
        mock_release_resp.json.return_value = {"status": "RELEASED"}

        def mock_post(url, *args, **kwargs):
            if "charge" in url:
                return mock_charge_resp
            if "void" in url:
                mock_void = MagicMock()
                mock_void.status_code = 200
                mock_void.json.return_value = {"status": "VOIDED"}
                return mock_void
            if "release" in url:
                return mock_release_resp
            raise ValueError(f"Unexpected POST url: {url}")

        with patch("coordinator.views._http_post", side_effect=mock_post), \
             patch("coordinator.views._http_get") as p_get:
            _charge(
                transaction_id=self.tx_id,
                amount=Decimal("100"),
                seat_ids=[1],
            )
            p_get.assert_not_called()
