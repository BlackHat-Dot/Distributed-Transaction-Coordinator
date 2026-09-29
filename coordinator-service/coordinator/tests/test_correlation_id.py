import logging
import uuid
from unittest.mock import MagicMock, patch
from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from coordinator.models import Transaction
from coordinator.middleware import CorrelationIdFilter


class CorrelationIdMiddlewareTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_incoming_client_correlation_id_is_ignored_and_server_generates_fresh_uuid(self):
        client_cid = "test-corr-id-" + str(uuid.uuid4())
        resp = self.client.get("/health/", HTTP_X_CORRELATION_ID=client_cid)
        self.assertEqual(resp.status_code, 200)
        server_cid = resp.get("X-Correlation-ID")
        self.assertTrue(server_cid)
        self.assertNotEqual(server_cid, client_cid)
        parsed = uuid.UUID(server_cid)
        self.assertEqual(parsed.version, 4)

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
        captured_headers = []

        def mock_post(url, *args, **kwargs):
            captured_headers.append((url, kwargs.get("headers", {})))
            mock = MagicMock()
            mock.status_code = 200
            if "pricing" in url:
                mock.json.return_value = [{"id": 1, "price": 500, "row": "A", "seat_number": "1"}]
            elif "reserve" in url:
                mock.json.return_value = {"message": "Seat reserved", "seat_ids": [1]}
            elif "charge" in url:
                mock.json.return_value = {"status": "CHARGED"}
            elif "confirm" in url:
                mock.json.return_value = {"message": "Seat confirmed", "seat_ids": [1]}
            return mock

        with patch("coordinator.views.requests.post", side_effect=mock_post):
            resp = self.client.post(
                "/transactions/?sync=true",
                {"seat_ids": [1]},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        server_cid = resp.get("X-Correlation-ID")
        self.assertTrue(server_cid)
        self.assertEqual(len(captured_headers), 4)  # pricing + reserve + charge + confirm

        for url, headers in captured_headers:
            self.assertEqual(
                headers.get("X-Correlation-ID"),
                server_cid,
                f"Missing or mismatched X-Correlation-ID for outbound call to {url}",
            )

    def test_recovery_command_sends_correlation_id_on_probes_and_compensation(self):
        tid = str(uuid.uuid4())
        txn = Transaction.objects.create(
            transaction_id=tid,
            status=Transaction.Status.PAYMENT_PROCESSING,
            seat_ids=[1],
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
            if "void" in url:
                mock.json.return_value = {"status": "VOIDED"}
            else:
                mock.json.return_value = {"message": "Inventory released"}
            return mock

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=mock_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=mock_post):
            call_command("recovery_stuck_transactions")

        # 3 probes: 1) payment check in _resolve_transaction, 2) inventory check in _resolve_transaction, 3) payment check in execute_compensation_sweep
        self.assertEqual(len(captured_probe_headers), 3)
        for url, headers in captured_probe_headers:
            self.assertEqual(
                headers.get("X-Correlation-ID"),
                tid,
                f"Recovery probe to {url} did not send X-Correlation-ID",
            )

        self.assertEqual(len(captured_release_headers), 2)
        for url, headers in captured_release_headers:
            self.assertEqual(
                headers.get("X-Correlation-ID"),
                tid,
                f"Recovery compensation call to {url} did not send X-Correlation-ID",
            )
