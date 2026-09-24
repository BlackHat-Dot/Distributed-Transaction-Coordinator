import uuid
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.test import TestCase
import requests

from coordinator.models import Transaction, SagaEvent
from coordinator.views import _compensate, retry_compensation, trigger_recovery_sweep
from rest_framework.test import APIRequestFactory


class SecurityAndReliabilityHardeningTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    @patch("coordinator.views.requests.post")
    def test_rel01_refund_is_dispatched_even_when_inventory_release_fails(self, mock_post):
        """
        REL-01: Proves that when money was charged, a payment refund request is dispatched
        even if releasing inventory suffers a network drop or returns HTTP 500.
        """
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.PAYMENT_PROCESSING,
            amount=Decimal("500.00"),
            seat_ids=[10],
            user_id="usr_test_victim",
            user_email="victim@example.com",
        )

        # Mock inventory release failing with ConnectionError
        # Mock payment refund succeeding with HTTP 200
        def side_effect(url, **kwargs):
            mock_resp = MagicMock()
            if "seats/release" in url:
                raise requests.exceptions.ConnectionError("Inventory connection dropped")
            elif "payments/refund" in url:
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"status": "REFUNDED", "transaction_id": str(tx_id)}
                return mock_resp
            mock_resp.status_code = 404
            return mock_resp

        mock_post.side_effect = side_effect

        # Call _compensate with was_charged=True (payment_status_code=200)
        payment_data = {"status": "CHARGED", "transaction_id": str(tx_id)}
        response = _compensate(
            transaction_id=tx_id,
            fail_compensation=False,
            payment_data=payment_data,
            payment_status_code=200,
            correlation_id="corr-test-rel01",
            seat_ids=[10],
        )

        # 1. Verify response is 503 because release network failed
        self.assertEqual(response.status_code, 503)
        # 2. Crucial: verify that payment refund was indeed called!
        post_urls = [call.args[0] for call in mock_post.call_args_list]
        self.assertTrue(any("payments/refund" in url for url in post_urls), "Payment refund was NOT dispatched!")
        # 3. Verify refund response is attached to payload so caller knows money was refunded
        self.assertEqual(response.data.get("refund_response"), {"status": "REFUNDED", "transaction_id": str(tx_id)})
        # 4. Verify transaction ended in COMPENSATION_FAILED so recovery daemon can retry seat release
        txn = Transaction.objects.get(transaction_id=tx_id)
        self.assertEqual(txn.status, Transaction.Status.COMPENSATION_FAILED)
