import os
import uuid
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.test import TestCase
import requests

from coordinator.models import Transaction, SagaEvent
from coordinator.views import (
    _compensate,
    retry_compensation,
    trigger_recovery_sweep,
    list_transactions,
    get_transaction,
    list_transaction_events,
)
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

    @patch("coordinator.views.requests.post")
    @patch("coordinator.views._get_requester_identity")
    def test_sec01_retry_compensation_idor_rejected(self, mock_identity, mock_post):
        """
        SEC-01: Proves that a non-staff caller cannot trigger retry-compensation
        on a transaction owned by another user (BOLA/IDOR protection).
        """
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.COMPENSATION_FAILED,
            amount=Decimal("350.00"),
            seat_ids=[5],
            user_id="usr_victim_123",
            user_email="victim@example.com",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "RELEASED"}
        mock_post.return_value = mock_resp

        # 1. Attacker (non-staff, different user_id) tries to trigger retry_compensation
        mock_identity.return_value = (True, "usr_attacker_666", False)
        request = self.factory.post(f"/transactions/{tx_id}/retry-compensation/")
        response = retry_compensation(request, tx_id)

        self.assertEqual(response.status_code, 403)
        self.assertIn("error", response.data)
        self.assertIn("permission", response.data["error"].lower())

        # 2. Owner (non-staff, matching user_id) triggers retry_compensation -> Allowed
        mock_identity.return_value = (True, "usr_victim_123", False)
        request = self.factory.post(f"/transactions/{tx_id}/retry-compensation/")
        response = retry_compensation(request, tx_id)
        self.assertEqual(response.status_code, 200)

        # Reset transaction status back to COMPENSATION_FAILED for staff test
        Transaction.objects.filter(transaction_id=tx_id).update(status=Transaction.Status.COMPENSATION_FAILED)

        # 3. Staff user (is_staff=True) triggers retry_compensation -> Allowed
        mock_identity.return_value = (True, "admin_user", True)
        request = self.factory.post(f"/transactions/{tx_id}/retry-compensation/")
        response = retry_compensation(request, tx_id)
        self.assertEqual(response.status_code, 200)

    @patch("django.core.management.call_command")
    @patch("coordinator.views._get_requester_identity")
    def test_sec02_recovery_sweep_requires_staff_in_production(self, mock_identity, mock_call_cmd):
        """
        SEC-02: Proves that in production (DEBUG=False, IS_TESTING=False),
        non-staff callers are denied access (403) to trigger recovery sweep,
        while staff callers are allowed. In dev/testing, execution is permitted.
        """
        # 1. Unauthenticated in prod -> 401
        with self.settings(DEBUG=False, IS_TESTING=False):
            mock_identity.return_value = (False, None, False)
            request = self.factory.post("/recovery/sweep/", {"threshold_seconds": 30}, format="json")
            response = trigger_recovery_sweep(request)
            self.assertEqual(response.status_code, 401)
            mock_call_cmd.assert_not_called()

        # 2. Authenticated non-staff in prod -> 403
        with self.settings(DEBUG=False, IS_TESTING=False):
            mock_identity.return_value = (True, "usr_normal_user", False)
            request = self.factory.post("/recovery/sweep/", {"threshold_seconds": 30}, format="json")
            response = trigger_recovery_sweep(request)
            self.assertEqual(response.status_code, 403)
            self.assertIn("Staff privileges required", response.data.get("error", ""))
            mock_call_cmd.assert_not_called()

        # 3. Authenticated staff in prod -> 200
        with self.settings(DEBUG=False, IS_TESTING=False):
            mock_identity.return_value = (True, "admin_user", True)
            request = self.factory.post("/recovery/sweep/", {"threshold_seconds": 30}, format="json")
            response = trigger_recovery_sweep(request)
            self.assertEqual(response.status_code, 200)
            mock_call_cmd.assert_called_once()

        # 4. Dev / Testing mode -> Allowed without staff check
        mock_call_cmd.reset_mock()
        with self.settings(DEBUG=True, IS_TESTING=False):
            request = self.factory.post("/recovery/sweep/", {"threshold_seconds": 30}, format="json")
            response = trigger_recovery_sweep(request)
            self.assertEqual(response.status_code, 200)
            mock_call_cmd.assert_called_once()

    @patch.dict(os.environ, {"STAFF_API_KEY": "super-secret-staff-key-xyz"})
    def test_sec04_constant_time_secret_comparison(self):
        """
        SEC-04: Proves that staff key authentication validates keys
        and rejects forged or prefix-matched keys.
        """
        from coordinator.views import _get_requester_identity

        # Valid staff key
        req_valid = self.factory.get("/transactions/", HTTP_X_STAFF_KEY="super-secret-staff-key-xyz")
        is_auth, uid, is_staff = _get_requester_identity(req_valid)
        self.assertTrue(is_auth)
        self.assertTrue(is_staff)
        self.assertEqual(uid, "system_staff")

        # Wrong key
        with self.settings(DEBUG=False, IS_TESTING=False):
            req_invalid = self.factory.get("/transactions/", HTTP_X_STAFF_KEY="super-secret-staff-key-wrong")
            is_auth, uid, is_staff = _get_requester_identity(req_invalid)
            self.assertFalse(is_auth)
            self.assertFalse(is_staff)

            # Prefix attack
            req_prefix = self.factory.get("/transactions/", HTTP_X_STAFF_KEY="super-secret-staff-key")
            is_auth, uid, is_staff = _get_requester_identity(req_prefix)
            self.assertFalse(is_auth)
            self.assertFalse(is_staff)

    @patch("coordinator.views._get_requester_identity")
    def test_sec07_falsy_user_id_does_not_leak_cross_tenant_transactions(self, mock_identity):
        """
        SEC-07: Proves that non-staff callers with empty or falsy user_ids
        cannot view all transactions (cross-tenant leak) or inspect other users'
        transaction details and events.
        """
        tx1 = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.COMPLETED,
            amount=Decimal("100.00"),
            seat_ids=[1],
            user_id="usr_alice",
            user_email="alice@example.com",
        )
        tx2 = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.COMPLETED,
            amount=Decimal("200.00"),
            seat_ids=[2],
            user_id="usr_bob",
            user_email="bob@example.com",
        )

        # 1. Non-staff caller with falsy/empty user ID lists transactions
        mock_identity.return_value = (True, "", False)
        request = self.factory.get("/transactions/")
        response = list_transactions(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(len(response.data["results"]), 0)

        # 2. Non-staff caller with falsy user ID attempts to view Alice's transaction details
        mock_identity.return_value = (True, "", False)
        request = self.factory.get(f"/transactions/{tx1.transaction_id}/")
        response = get_transaction(request, tx1.transaction_id)
        self.assertEqual(response.status_code, 403)

        # 3. Non-staff caller with falsy user ID attempts to view Alice's events
        request = self.factory.get(f"/transactions/{tx1.transaction_id}/events/")
        response = list_transaction_events(request, tx1.transaction_id)
        self.assertEqual(response.status_code, 403)

    def test_sec05_spoofed_x_forwarded_for_is_ignored(self):
        """
        SEC-05: Proves that spoofed X-Forwarded-For headers from untrusted
        remote clients are ignored to prevent IP rate-limit bypass.
        """
        from coordinator.auth_views import _get_client_ip

        # 1. Untrusted caller directly connects with spoofed XFF
        req_untrusted = self.factory.get(
            "/auth/send-otp/",
            REMOTE_ADDR="198.51.100.42",
            HTTP_X_FORWARDED_FOR="203.0.113.99, 1.1.1.1",
        )
        resolved_ip = _get_client_ip(req_untrusted)
        self.assertEqual(
            resolved_ip,
            "198.51.100.42",
            "Spoofed XFF was accepted from an untrusted peer!",
        )

        # 2. Trusted proxy connects on behalf of real client
        with self.settings(TRUSTED_PROXIES={"10.0.0.1", "127.0.0.1"}):
            req_trusted = self.factory.get(
                "/auth/send-otp/",
                REMOTE_ADDR="10.0.0.1",
                HTTP_X_FORWARDED_FOR="198.51.100.77, 10.0.0.1",
            )
            resolved_ip = _get_client_ip(req_trusted)
            self.assertEqual(resolved_ip, "198.51.100.77")

    @patch("coordinator.saga_log._get_pub_client")
    def test_sse01_publish_transaction_update_dispatches_redis_event(self, mock_get_client):
        """
        SSE-01: Proves that _publish_transaction_update publishes a JSON event
        to the Redis pub/sub channel for real-time frontend streaming.
        """
        from coordinator.saga_log import _publish_transaction_update
        mock_redis = MagicMock()
        mock_get_client.return_value = mock_redis

        tx_id = uuid.uuid4()
        _publish_transaction_update(tx_id, "PAYMENT_PROCESSING", "INVENTORY_RESERVED")

        mock_redis.publish.assert_called_once()
        channel, message = mock_redis.publish.call_args[0]
        self.assertEqual(channel, f"dtc:tx_stream:{tx_id}")

        import json
        payload = json.loads(message)
        self.assertEqual(payload["transaction_id"], str(tx_id))
        self.assertEqual(payload["status"], "PAYMENT_PROCESSING")
        self.assertEqual(payload["from_status"], "INVENTORY_RESERVED")
        self.assertFalse(payload["is_terminal"])

    @patch("coordinator.views._get_requester_identity")
    def test_sse02_transaction_stream_idor_protection(self, mock_identity):
        """
        SSE-02: Proves that non-staff callers cannot open an SSE stream
        for transactions belonging to another user.
        """
        from coordinator.views import transaction_stream
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="user_alice",
            status=Transaction.Status.STARTED,
            amount=Decimal("500.00"),
        )

        # Bob attempts to stream Alice's transaction
        mock_identity.return_value = (True, "user_bob", False)
        request = self.factory.get(f"/transactions/{tx_id}/stream/")
        response = transaction_stream(request, tx_id)
        self.assertEqual(response.status_code, 403)

    @patch("coordinator.views._get_requester_identity")
    def test_sse03_transaction_stream_yields_terminal_state_immediately(self, mock_identity):
        """
        SSE-03: Proves that if a transaction is already terminal when the SSE stream
        connects, the generator immediately yields the terminal state and terminates.
        """
        from coordinator.views import transaction_stream
        import json
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="user_alice",
            status=Transaction.Status.COMPLETED,
            amount=Decimal("500.00"),
        )

        mock_identity.return_value = (True, "user_alice", False)
        request = self.factory.get(f"/transactions/{tx_id}/stream/")
        response = transaction_stream(request, tx_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")

        # Read chunks from generator
        chunks = list(response.streaming_content)
        self.assertTrue(len(chunks) >= 1)
        first_chunk = chunks[0]
        if isinstance(first_chunk, bytes):
            first_chunk = first_chunk.decode("utf-8")
        self.assertTrue(first_chunk.startswith("data: "))
        data = json.loads(first_chunk.replace("data: ", "").strip())
        self.assertEqual(data["status"], "COMPLETED")
        self.assertTrue(data["is_terminal"])





