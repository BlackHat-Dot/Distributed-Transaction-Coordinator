import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch
import requests
from django.test import TestCase
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent
from coordinator.views import _compensate, _finalize_payment_outcome
from coordinator.recovery import execute_compensation_sweep


class CleanupA8Tests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_compensate_dead_branch_removed_f09(self):
        """F-09: payment_status_code==200 must not trigger refund if payment_data does not indicate CHARGED."""
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.PAYMENT_PROCESSING,
            seat_ids=[1],
            quantity=1,
            amount=Decimal("500"),
        )
        mock_release = MagicMock()
        mock_release.status_code = 200
        mock_release.content = b'{"status": "released"}'
        mock_release.json.return_value = {"status": "released"}

        mock_refund = MagicMock()
        mock_refund.status_code = 200

        with patch("coordinator.views._http_post") as mock_http_post:
            def side_effect(url, **kwargs):
                if "release" in url:
                    return mock_release
                if "refund" in url:
                    return mock_refund
                return MagicMock(status_code=200)

            mock_http_post.side_effect = side_effect

            # payment_data is a decline or non-dict, but payment_status_code is 200
            resp = _compensate(
                txn.transaction_id,
                fail_compensation=False,
                payment_data={"status": "DECLINED"},
                payment_status_code=200,
            )

            # Refund must NOT have been called
            refund_calls = [c for c in mock_http_post.call_args_list if "refund" in c.args[0]]
            self.assertEqual(len(refund_calls), 0, "Refund was erroneously called for non-CHARGED payment!")

    def test_finalize_payment_outcome_sanitizes_confirm_err_f43(self):
        """F-43: Raw Python exception from _confirm_inventory must not leak to the user payload."""
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.PAYMENT_PROCESSING,
            seat_ids=[1],
            quantity=1,
            amount=Decimal("500"),
        )
        raw_error_text = "Connection refused to http://internal-inventory:8000/seats/confirm/ [SECRET_SOCKET_FD_99]"

        with patch("coordinator.views._confirm_inventory", return_value=(None, requests.exceptions.ConnectionError(raw_error_text))), \
             patch("coordinator.views._compensate") as mock_compensate:
            mock_compensate.return_value = MagicMock(status_code=200)

            _finalize_payment_outcome(
                txn.transaction_id,
                fail_compensation=False,
                payment_data={"status": "CHARGED"},
            )

            self.assertTrue(mock_compensate.called)
            decline_payload = mock_compensate.call_args.args[2]
            inv_confirm = decline_payload.get("inventory_confirm")
            self.assertNotIn(raw_error_text, str(inv_confirm), "Raw exception details leaked into decline_payload!")

    def test_retry_compensation_release_already_released_treated_as_success_f43(self):
        """F-43: Non-200 inventory release with 'already released' must be treated as success."""
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.RECOVERY_IN_PROGRESS,
            seat_ids=[1],
            quantity=1,
            amount=Decimal("500"),
        )
        mock_check = MagicMock()
        mock_check.status_code = 200
        mock_check.content = b'{"status": "NOT_CHARGED"}'
        mock_check.json.return_value = {"status": "NOT_CHARGED"}

        mock_release = MagicMock()
        mock_release.status_code = 409
        mock_release.content = b'{"error": "Reservation already released"}'
        mock_release.json.return_value = {"error": "Reservation already released"}

        outcome = execute_compensation_sweep(
            transaction_id=txn.transaction_id,
            actor=SagaEvent.ACTOR_RETRY,
            original_status=Transaction.Status.COMPENSATION_FAILED,
            get_func=lambda *args, **kwargs: mock_check,
            post_func=lambda *args, **kwargs: mock_release,
        )

        self.assertTrue(outcome["success"], f"Outcome was expected to succeed but failed: {outcome}")
        self.assertEqual(outcome["status_code"], 200)
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED_ON_RETRY)
