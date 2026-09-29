from datetime import timedelta
import os
import uuid
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone
from django.core.management import call_command
from rest_framework.test import APIClient

from coordinator.models import Transaction


class RetryCompensationA1Tests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tx_id = uuid.uuid4()
        self.url = f"/transactions/{self.tx_id}/retry-compensation/"

    @override_settings(IS_TESTING=False, DEBUG=False)
    @patch.dict(os.environ, {"ADMIN_DOMAIN": "@admin.org"})
    @patch("coordinator.views.get_user_from_request")
    def test_retry_compensation_staff_only_non_staff_rejected(self, mock_jwt):
        """Non-staff authenticated user must receive 403 Forbidden."""
        mock_jwt.return_value = {
            "is_authenticated": True,
            "user_id": "regular_user",
            "user_email": "user@customer.com",
        }
        _txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.COMPENSATION_FAILED,
            seat_ids=[1],
            quantity=1,
            amount=100,
            user_id="regular_user",
        )
        resp = self.client.post(self.url, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_retry_compensation_unknown_id_returns_404(self):
        """Unknown transaction IDs must return 404, not status disclosure."""
        unknown_id = uuid.uuid4()
        url = f"/transactions/{unknown_id}/retry-compensation/"
        resp = self.client.post(url, format="json")
        self.assertEqual(resp.status_code, 404)

    def test_retry_compensation_unallowed_status_rejected(self):
        """Transactions not in allowlist must be rejected with 400 Bad Request."""
        _txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.STARTED,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )
        resp = self.client.post(self.url, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_retry_compensation_short_circuit_compensated(self):
        """Transactions already in COMPENSATED or COMPENSATED_ON_RETRY short-circuit to 200 without calls."""
        for st in (Transaction.Status.COMPENSATED, Transaction.Status.COMPENSATED_ON_RETRY):
            tid = uuid.uuid4()
            Transaction.objects.create(
                transaction_id=tid,
                status=st,
                seat_ids=[1],
                quantity=1,
                amount=100,
            )
            with patch("coordinator.views.requests.post") as mock_post, \
                 patch("coordinator.views.requests.get") as mock_get:
                resp = self.client.post(f"/transactions/{tid}/retry-compensation/", format="json")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.data.get("status"), st)
                mock_post.assert_not_called()
                mock_get.assert_not_called()

    def test_retry_compensation_payment_check_5xx_releases_claim_and_returns_503(self):
        """Payment check 5xx/timeout releases claim back to original status, returns 503, never releases inventory."""
        txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.COMPENSATION_FAILED,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )
        mock_check = MagicMock()
        mock_check.status_code = 500
        mock_check.content = b'{"error": "Internal Error"}'
        mock_check.json.return_value = {"error": "Internal Error"}

        mock_post_obj = MagicMock()
        mock_post_obj.status_code = 200
        mock_post_obj.content = b'{}'
        mock_post_obj.json.return_value = {}

        with patch("coordinator.views.requests.get", return_value=mock_check), \
             patch("coordinator.views.requests.post", return_value=mock_post_obj) as mock_post:
            resp = self.client.post(self.url, format="json")
            self.assertEqual(resp.status_code, 503)
            # Inventory release must NOT be called
            mock_post.assert_not_called()

        txn.refresh_from_db()
        # Must be reverted back to COMPENSATION_FAILED so sweeper can retry
        self.assertEqual(txn.status, Transaction.Status.COMPENSATION_FAILED)

    def test_retry_compensation_charged_triggers_refund_and_release(self):
        """Payment status CHARGED triggers refund, releases inventory, transitions to COMPENSATED_ON_RETRY."""
        txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.COMPENSATION_FAILED,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )
        def mock_get(url, *args, **kwargs):
            mock = MagicMock()
            mock.status_code = 200
            mock.content = b'{"status": "CHARGED"}'
            mock.json.return_value = {"status": "CHARGED"}
            return mock

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "confirm" in url:
                mock.status_code = 409
                mock.content = b'{"error": "Hold expired", "code": "HOLD_EXPIRED"}'
                mock.json.return_value = {"error": "Hold expired", "code": "HOLD_EXPIRED"}
            elif "refund" in url:
                mock.status_code = 200
                mock.content = b'{"status": "REFUNDED"}'
                mock.json.return_value = {"status": "REFUNDED"}
            else:
                mock.status_code = 200
                mock.content = b'{"message": "Released"}'
                mock.json.return_value = {"message": "Released"}
            return mock

        with patch("coordinator.views.requests.get", side_effect=mock_get), \
             patch("coordinator.views.requests.post", side_effect=mock_post):
            resp = self.client.post(self.url, format="json")
            self.assertEqual(resp.status_code, 200)

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED_ON_RETRY)

    def test_sweeper_handles_retry_compensation_failed(self):
        """Sweeper must include RETRY_COMPENSATION_FAILED in recovery candidates."""
        txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.RETRY_COMPENSATION_FAILED,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )
        past_time = timezone.now() - timedelta(minutes=10)
        Transaction.objects.filter(transaction_id=self.tx_id).update(updated_at=past_time)

        def mock_get(url, *args, **kwargs):
            mock = MagicMock()
            mock.status_code = 200
            mock.content = b'{"status": "NOT_CHARGED"}'
            mock.json.return_value = {"status": "NOT_CHARGED"}
            return mock

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            mock.status_code = 200
            mock.content = b'{"message": "Released"}'
            mock.json.return_value = {"message": "Released"}
            return mock

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=mock_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=mock_post):
            call_command("recovery_stuck_transactions")

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED)
