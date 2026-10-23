import uuid
from unittest.mock import MagicMock, patch

from django.test import TestCase

from coordinator.models import Transaction
from coordinator.views import _compensate, _finish_payment


class CasChecksA2Tests(TestCase):
    def setUp(self):
        self.tx_id = uuid.uuid4()

    def test_compensate_lost_cas_does_not_release_or_refund_returns_409(self):
        """F-41: If _compensate loses CAS claim to COMPENSATING, it must return 409 and never call release or refund."""
        # Transaction is already moved to COMPLETED or RECOVERY_IN_PROGRESS by sweeper
        txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.COMPLETED,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )

        with patch("coordinator.views.requests.post") as mock_post, \
             patch("coordinator.views._http_post") as mock_http_post:
            resp = _compensate(
                transaction_id=self.tx_id,
                payment_data={"status": "CHARGED"},
                payment_status_code=422,
                seat_ids=[1],
            )
            self.assertEqual(resp.status_code, 409)
            mock_post.assert_not_called()
            mock_http_post.assert_not_called()

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPLETED)

    def test_finish_payment_lost_cas_returns_409(self):
        """F-42: If _finish_payment loses CAS claim to COMPLETED, it must return 409 instead of 200."""
        # Transaction is already moved to COMPENSATION_FAILED
        _txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.COMPENSATION_FAILED,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )

        mock_confirm_resp = MagicMock()
        mock_confirm_resp.status_code = 200
        mock_confirm_resp.json.return_value = {"status": "CONFIRMED"}

        with patch("coordinator.views._confirm", return_value=(mock_confirm_resp, None)):
            resp = _finish_payment(
                transaction_id=self.tx_id,
                payment_data={"status": "CHARGED"},
                payment_status_code=200,
                seat_ids=[1],
            )
            self.assertEqual(resp.status_code, 409)
