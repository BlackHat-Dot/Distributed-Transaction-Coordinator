import logging
import uuid
from unittest.mock import MagicMock, patch
from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from coordinator.models import Transaction
from coordinator.middleware import CorrelationIdFilter, get_correlation_id


class CorrelationIdMiddlewareTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_incoming_correlation_id_is_returned_in_response_header(self):
        cid = "test-corr-id-" + str(uuid.uuid4())
        resp = self.client.get("/health/", HTTP_X_CORRELATION_ID=cid)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get("X-Correlation-ID"), cid)

    def test_missing_correlation_id_uses_transaction_id_or_generates_uuid(self):
        resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 200)
        cid = resp.get("X-Correlation-ID")
        self.assertTrue(cid)
        uuid.UUID(cid)

    def test_logging_filter_injects_correlation_id(self):
        filter_obj = CorrelationIdFilter()
        logger = logging.getLogger("coordinator.test")
        record = logger.makeRecord("coordinator.test", logging.INFO, "test.py", 1, "test message", (), None)

        filter_obj.filter(record)
        self.assertTrue(hasattr(record, "correlation_id"))


class CorrelationIdOutboundPropagationTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()

    def test_forward_saga_propagates_correlation_id_in_outbound_requests(self):
        tid = str(uuid.uuid4())
        custom_cid = f"trace-{tid}"
        captured_headers = []

        def mock_post(url, *args, **kwargs):
            captured_headers.append((url, kwargs.get("headers", {})))
            mock = MagicMock()
            mock.status_code = 200
            if "reserve" in url:
                mock.json.return_value = {"message": "Seat reserved", "seat_id": 1}
            elif "charge" in url:
                mock.json.return_value = {"status": "CHARGED", "transaction_id": tid}
            elif "confirm" in url:
                mock.json.return_value = {"message": "Seat confirmed", "seat_id": 1}
            return mock

        with patch("coordinator.views.requests.post", side_effect=mock_post):
            resp = self.client.post(
                "/transactions/",
                {"transaction_id": tid, "seat_id": 1, "amount": 500},
                format="json",
                HTTP_X_CORRELATION_ID=custom_cid,
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get("X-Correlation-ID"), custom_cid)
        self.assertEqual(len(captured_headers), 3)

        for url, headers in captured_headers:
            self.assertEqual(
                headers.get("X-Correlation-ID"),
                custom_cid,
                f"Missing or mismatched X-Correlation-ID for outbound call to {url}",
            )

    def test_recovery_command_sends_correlation_id_on_probes_and_compensation(self):
        tid = str(uuid.uuid4())
        txn = Transaction.objects.create(
            transaction_id=tid,
            status=Transaction.Status.PAYMENT_PROCESSING,
            product_id=1,
            quantity=1,
            amount=500,
        )
        past_time = timezone.now() - timedelta(minutes=10)
        Transaction.objects.filter(pk=txn.pk).update(updated_at=past_time)

        captured_probe_headers = []
        captured_release_headers = []

        def mock_get(url, *args, **kwargs):
            captured_probe_headers.append((url, kwargs.get("headers", {})))
            mock = MagicMock()
            mock.status_code = 200
            if "payments/check" in url:
                mock.json.return_value = {"status": "NOT_CHARGED"}
            elif "reserve/check" in url:
                mock.json.return_value = {"status": Transaction.Status.INVENTORY_RESERVED}
            return mock

        def mock_post(url, *args, **kwargs):
            captured_release_headers.append((url, kwargs.get("headers", {})))
            mock = MagicMock()
            mock.status_code = 200
            mock.json.return_value = {"message": "Inventory released"}
            return mock

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=mock_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=mock_post):
            call_command("recovery_stuck_transactions")

        self.assertEqual(len(captured_probe_headers), 2)
        for url, headers in captured_probe_headers:
            self.assertEqual(
                headers.get("X-Correlation-ID"),
                tid,
                f"Recovery probe to {url} did not send X-Correlation-ID",
            )

        self.assertEqual(len(captured_release_headers), 1)
        for url, headers in captured_release_headers:
            self.assertEqual(
                headers.get("X-Correlation-ID"),
                tid,
                f"Recovery compensation release to {url} did not send X-Correlation-ID",
            )
