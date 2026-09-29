import os
import uuid
import json
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

    @patch("coordinator.views.requests.get")
    @patch("coordinator.views.requests.post")
    @patch("coordinator.views._get_requester_identity")
    def test_sec01_retry_compensation_idor_rejected(self, mock_identity, mock_post, mock_get):
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

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {"status": "NOT_CHARGED"}
        mock_get.return_value = mock_get_resp

        # 1. Attacker (non-staff, different user_id) tries to trigger retry_compensation
        mock_identity.return_value = (True, "usr_attacker_666", False)
        request = self.factory.post(f"/transactions/{tx_id}/retry-compensation/")
        response = retry_compensation(request, tx_id)

        self.assertEqual(response.status_code, 403)
        self.assertIn("error", response.data)
        self.assertIn("permission", response.data["error"].lower())

        # 2. Owner (non-staff, matching user_id) triggers retry_compensation -> Forbidden (staff required per A1)
        mock_identity.return_value = (True, "usr_victim_123", False)
        request = self.factory.post(f"/transactions/{tx_id}/retry-compensation/")
        response = retry_compensation(request, tx_id)
        self.assertEqual(response.status_code, 403)

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

    @patch("coordinator.views._http_post")
    def test_compensate_calls_void_when_not_charged(self, mock_http_post):
        """
        Verify that _compensate invokes POST /payments/void/ when payment was not charged.
        """
        from coordinator.views import _compensate
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "NOT_CHARGED", "voided": True}
        mock_http_post.return_value = mock_response

        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="user_alice",
            status=Transaction.Status.PAYMENT_PROCESSING,
            amount=Decimal("500.00"),
        )

        _compensate(
            transaction_id=tx_id,
            fail_compensation=False,
            payment_data={"status": "NOT_CHARGED"},
            payment_status_code=422,
        )

        # Check that one of the calls was to /payments/void/
        void_calls = [
            call for call in mock_http_post.call_args_list
            if "/payments/void/" in call[0][0]
        ]
        self.assertTrue(len(void_calls) >= 1)
        self.assertEqual(void_calls[0][1]["json"]["transaction_id"], str(tx_id))

    @patch("coordinator.views._http_post")
    def test_compensate_flips_to_refund_if_void_returns_charged(self, mock_http_post):
        """
        Verify that if /payments/void/ reports the charge landed during timeout,
        was_charged flips to True and refund is dispatched.
        """
        from coordinator.views import _compensate

        def side_effect(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/payments/void/" in url:
                resp.json.return_value = {"status": "CHARGED", "amount": "500.00"}
            elif "/payments/refund/" in url:
                resp.json.return_value = {"status": "REFUNDED", "amount": "500.00"}
            else:
                resp.json.return_value = {"released_count": 1}
            return resp

        mock_http_post.side_effect = side_effect

        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="user_alice",
            status=Transaction.Status.PAYMENT_PROCESSING,
            amount=Decimal("500.00"),
        )

        _compensate(
            transaction_id=tx_id,
            fail_compensation=False,
            payment_data={"status": "NOT_CHARGED"},
            payment_status_code=504,
        )

        refund_calls = [
            call for call in mock_http_post.call_args_list
            if "/payments/refund/" in call[0][0]
        ]
        self.assertTrue(len(refund_calls) >= 1)

    def test_h1_recovery_sweep_calls_void_when_not_charged(self):
        """
        H1 / N1: Proves that execute_compensation_sweep calls /payments/void/
        when payment status is NOT_CHARGED before releasing inventory.
        """
        from coordinator.recovery import execute_compensation_sweep
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="user_bob",
            status=Transaction.Status.RECOVERY_IN_PROGRESS,
            amount=Decimal("150.00"),
            seat_ids=[1],
        )

        mock_get = MagicMock()
        mock_get.status_code = 200
        mock_get.content = b'{"status": "NOT_CHARGED"}'
        mock_get.json.return_value = {"status": "NOT_CHARGED"}

        mock_post_calls = []
        def mock_post(url, **kwargs):
            mock_post_calls.append(url)
            resp = MagicMock()
            resp.status_code = 200
            if "/payments/void/" in url:
                resp.content = b'{"status": "VOIDED"}'
                resp.json.return_value = {"status": "VOIDED"}
            else:
                resp.content = b'{"status": "RELEASED"}'
                resp.json.return_value = {"status": "RELEASED"}
            return resp

        outcome = execute_compensation_sweep(
            transaction_id=tx_id,
            actor=SagaEvent.ACTOR_RECOVERY,
            get_func=lambda url, **kwargs: mock_get,
            post_func=mock_post,
        )

        self.assertTrue(
            any("/payments/void/" in url for url in mock_post_calls),
            "execute_compensation_sweep failed to call /payments/void/ when payment was NOT_CHARGED!",
        )
        self.assertTrue(outcome["success"])

    def test_h2_confirm_timeout_retried_before_compensating(self):
        """
        H2: When payment is CHARGED and _confirm_inventory times out,
        coordinator must retry _confirm_inventory before giving up / compensating.
        """
        from coordinator.views import _finalize_payment_outcome
        import requests

        tx_id = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            user_id="user_alice",
            status=Transaction.Status.PAYMENT_PROCESSING,
            amount=Decimal("100.00"),
            seat_ids=[1],
        )

        mock_confirm_ok = MagicMock()
        mock_confirm_ok.status_code = 200
        mock_confirm_ok.json.return_value = {"message": "Seats confirmed"}

        confirm_attempts = []
        def mock_confirm(transaction_id, seat_ids=None, correlation_id=None):
            confirm_attempts.append(len(confirm_attempts) + 1)
            if len(confirm_attempts) == 1:
                return None, requests.exceptions.Timeout("Read timeout on confirm")
            return mock_confirm_ok, None

        with patch("coordinator.views._confirm_inventory", side_effect=mock_confirm), \
             patch("coordinator.views._compensate") as mock_compensate:
            resp = _finalize_payment_outcome(
                tx_id,
                fail_compensation=False,
                payment_data={"status": "CHARGED"},
            )

            self.assertEqual(len(confirm_attempts), 2, "Expected _confirm_inventory to be retried on timeout")
            self.assertFalse(mock_compensate.called, "_compensate should not be called when retry succeeds")
            self.assertEqual(resp.status_code, 200)
            txn.refresh_from_db()
            self.assertEqual(txn.status, Transaction.Status.COMPLETED)

    def test_h3_hold_expiry_validated_before_charging_payment(self):
        """
        H3: The pay path must validate hold expiry (held_until) before charging.
        If the hold expired, payment must NOT be charged, and 409 HOLD_EXPIRED returned.
        """
        from datetime import timedelta
        from django.utils import timezone

        tx_id = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            user_id="user_charlie",
            user_email="charlie@test.io",
            status=Transaction.Status.HOLDING,
            amount=Decimal("200.00"),
            seat_ids=[1],
        )

        expired_time = (timezone.now() - timedelta(minutes=5)).isoformat()
        SagaEvent.objects.create(
            transaction=txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.HOLDING,
            actor=SagaEvent.ACTOR_REQUEST,
            http_status_code=200,
            http_payload={"held_until": expired_time, "seat_ids": [1]},
        )

        charge_called = []
        def mock_post(url, *args, **kwargs):
            if "charge" in url:
                charge_called.append(url)
            mock = MagicMock()
            mock.status_code = 200
            mock.json.return_value = {}
            return mock

        with patch("coordinator.views._http_post", side_effect=mock_post):
            resp = self.client.post(
                "/transactions/?sync=true",
                data={"transaction_id": str(tx_id), "action": "pay"},
                content_type="application/json",
                HTTP_X_USER_ID="user_charlie",
                HTTP_X_USER_EMAIL="charlie@test.io",
            )

            self.assertEqual(resp.status_code, 409)
            self.assertEqual(resp.data.get("code"), "HOLD_EXPIRED")
            self.assertEqual(len(charge_called), 0, "Payment charge was called for an expired hold!")
            txn.refresh_from_db()
            self.assertNotEqual(txn.status, Transaction.Status.HOLDING)

    def test_h4_explicit_simulate_decline_flag_allows_real_9999_amount_update(self):
        """
        H4: A real transaction with an amount around 9999 must get its authoritative amount
        updated from inventory even when ENABLE_TEST_PAYMENT_FAILURE is True, provided
        simulate_decline is False.
        """
        from coordinator.views import _run_saga_pipeline

        tx_id = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            user_id="user_david",
            status=Transaction.Status.STARTED,
            amount=Decimal("9999.00"),
            seat_ids=[1],
        )

        mock_reserve_resp = MagicMock()
        mock_reserve_resp.status_code = 200
        mock_reserve_resp.json.return_value = {
            "message": "1 Seats reserved",
            "seat_ids": [1],
            "total_amount": "9999.50",
            "transaction_id": str(tx_id),
        }

        mock_charge_resp = MagicMock()
        mock_charge_resp.status_code = 200
        mock_charge_resp.json.return_value = {"status": "CHARGED"}

        mock_confirm_resp = MagicMock()
        mock_confirm_resp.status_code = 200
        mock_confirm_resp.json.return_value = {"status": "CONFIRMED"}

        def mock_post(url, *args, **kwargs):
            if "reserve" in url:
                return mock_reserve_resp
            if "charge" in url:
                return mock_charge_resp
            if "confirm" in url:
                return mock_confirm_resp
            raise ValueError(f"Unexpected url: {url}")

        with self.settings(ENABLE_TEST_PAYMENT_FAILURE=True):
            with patch("coordinator.views._http_post", side_effect=mock_post):
                _run_saga_pipeline(
                    transaction_id=tx_id,
                    seat_ids=[1],
                    amount=Decimal("9999.00"),
                    user_id="user_david",
                    user_email="david@test.io",
                    simulate_decline=False,
                )

                txn.refresh_from_db()
                self.assertEqual(
                    txn.amount,
                    Decimal("9999.50"),
                    "Real order with amount 9999 was not updated from inventory!",
                )

    def test_h5_h6_centralized_terminal_and_recoverable_statuses(self):
        """
        H5/H6: Centralize terminal and recoverable statuses on Transaction model.
        Verify both sets, ensure INVENTORY_TIMEOUT / INVENTORY_CONNECTION_ERROR are present in TERMINAL_STATUSES,
        and ensure needs_recovery is present in _serialize_transaction and SSE events.
        """
        from coordinator.views import _serialize_transaction
        from coordinator.saga_log import _publish_transaction_update

        # 1. Model constants exist and are non-empty sets
        self.assertIsInstance(Transaction.TERMINAL_STATUSES, set)
        self.assertIsInstance(Transaction.RECOVERABLE_STATUSES, set)

        # 2. Key statuses verified in TERMINAL_STATUSES
        self.assertIn(Transaction.Status.INVENTORY_TIMEOUT, Transaction.TERMINAL_STATUSES)
        self.assertIn(Transaction.Status.INVENTORY_CONNECTION_ERROR, Transaction.TERMINAL_STATUSES)
        self.assertIn(Transaction.Status.PAYMENT_TIMEOUT, Transaction.TERMINAL_STATUSES)
        self.assertIn(Transaction.Status.COMPLETED, Transaction.TERMINAL_STATUSES)

        # 3. Key statuses verified in RECOVERABLE_STATUSES
        self.assertIn(Transaction.Status.PAYMENT_PROCESSING, Transaction.RECOVERABLE_STATUSES)
        self.assertIn(Transaction.Status.PAYMENT_TIMEOUT, Transaction.RECOVERABLE_STATUSES)
        self.assertIn(Transaction.Status.COMPENSATION_FAILED, Transaction.RECOVERABLE_STATUSES)
        self.assertIn(Transaction.Status.RETRY_COMPENSATION_FAILED, Transaction.RECOVERABLE_STATUSES)
        self.assertNotIn(Transaction.Status.COMPLETED, Transaction.RECOVERABLE_STATUSES)

        # 4. _serialize_transaction includes is_terminal and needs_recovery
        tx_id = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.PAYMENT_TIMEOUT,
            amount=Decimal("50.00"),
        )
        serialized = _serialize_transaction(txn)
        self.assertTrue(serialized["is_terminal"])
        self.assertTrue(serialized["needs_recovery"])

        txn.status = Transaction.Status.COMPLETED
        serialized_completed = _serialize_transaction(txn)
        self.assertTrue(serialized_completed["is_terminal"])
        self.assertFalse(serialized_completed["needs_recovery"])

        # 5. SSE event publishing includes needs_recovery
        published_payloads = []
        mock_client = MagicMock()
        def mock_publish(channel, payload):
            published_payloads.append(json.loads(payload))
        mock_client.publish = mock_publish

        with patch("coordinator.saga_log._get_pub_client", return_value=mock_client):
            _publish_transaction_update(tx_id, Transaction.Status.PAYMENT_TIMEOUT, Transaction.Status.PAYMENT_PROCESSING)
            self.assertEqual(len(published_payloads), 1)
            self.assertTrue(published_payloads[0]["is_terminal"])
            self.assertTrue(published_payloads[0]["needs_recovery"])

    def test_h7_retry_compensation_reverts_claim_on_unexpected_exception(self):
        """
        H7: When retry_compensation claims RECOVERY_IN_PROGRESS and execute_compensation_sweep
        raises an unhandled exception, coordinator must revert the claim to the original status
        and not leave the transaction stuck in RECOVERY_IN_PROGRESS.
        """
        tx_id = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            user_id="user_eve",
            status=Transaction.Status.COMPENSATION_FAILED,
            amount=Decimal("150.00"),
            seat_ids=[1],
        )

        request = self.factory.post(f"/transactions/{tx_id}/retry-compensation/")

        with patch("coordinator.views._get_requester_identity", return_value=(True, "user_eve", True)), \
             patch("coordinator.views.execute_compensation_sweep", side_effect=RuntimeError("Simulated sweep explosion")):
            resp = retry_compensation(request, tx_id)

            self.assertEqual(resp.status_code, 500)
            txn.refresh_from_db()
            self.assertEqual(
                txn.status,
                Transaction.Status.COMPENSATION_FAILED,
                "Claim was not reverted back to original status on unexpected error!",
            )

            # Audit events show claim + reversion
            events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
            self.assertTrue(len(events) >= 2)
            self.assertEqual(events[-2].to_status, Transaction.Status.RECOVERY_IN_PROGRESS)
            self.assertEqual(events[-1].to_status, Transaction.Status.COMPENSATION_FAILED)

    def test_h8_oneshot_celery_dispatch_failure_transitions_to_timeout(self):
        """
        H8: One-shot path: if execute_saga_task.delay fails, transaction must transition
        to TIMEOUT rather than remaining orphaned in STARTED.
        """
        mock_pricing_resp = MagicMock()
        mock_pricing_resp.status_code = 200
        mock_pricing_resp.json.return_value = [{"id": 1, "price": "100.00", "seat_number": "A1"}]

        with patch("coordinator.views._http_post", return_value=mock_pricing_resp), \
             patch("coordinator.tasks.execute_saga_task.delay", side_effect=RuntimeError("Celery connection refused")):
            resp = self.client.post(
                "/transactions/",
                data=json.dumps({"seat_ids": [1]}),
                content_type="application/json",
                HTTP_X_USER_ID="user_frank",
                HTTP_X_USER_EMAIL="frank@test.io",
            )

            self.assertEqual(resp.status_code, 503)
            tx_id = resp.data.get("transaction_id")
            self.assertIsNotNone(tx_id)
            txn = Transaction.objects.get(transaction_id=tx_id)
            self.assertEqual(txn.status, Transaction.Status.TIMEOUT)

            events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
            self.assertEqual(events[-1].to_status, Transaction.Status.TIMEOUT)
    def test_h11_auth_unification(self):
        """
        H11: requester identity unification:
        - _get_requester_identity returns RequesterIdentity tuple with .user_email attribute.
        - Unpacks cleanly into 3-tuple (is_auth, uid, is_staff).
        - _handle_create_transaction uses unified identity extraction and rejects unauthenticated callers in prod.
        """
        from coordinator.views import _get_requester_identity, RequesterIdentity

        # 1. Staff key extraction with email
        req_staff = self.factory.post(
            "/transactions/",
            HTTP_X_STAFF_KEY="secret-staff",
            HTTP_X_USER_ID="staff_agent_1",
            HTTP_X_USER_EMAIL="agent@admin.dtc.internal",
        )
        with patch.dict(os.environ, {"STAFF_API_KEY": "secret-staff"}):
            ident = _get_requester_identity(req_staff)
            self.assertIsInstance(ident, RequesterIdentity)
            is_auth, uid, is_staff = ident
            self.assertTrue(is_auth)
            self.assertEqual(uid, "staff_agent_1")
            self.assertTrue(is_staff)
            self.assertEqual(ident.user_email, "agent@admin.dtc.internal")

        # 2. JWT bearer extraction with admin check
        req_jwt = self.factory.post("/transactions/")
        with patch.dict(os.environ, {"ADMIN_DOMAIN": "dtc.internal"}), \
             patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "usr_99", "user_email": "boss@dtc.internal"}):
            ident = _get_requester_identity(req_jwt)
            is_auth, uid, is_staff = ident
            self.assertTrue(is_auth)
            self.assertEqual(uid, "usr_99")
            self.assertTrue(is_staff)
            self.assertEqual(ident.user_email, "boss@dtc.internal")

        # 3. In prod mode (DEBUG=False, IS_TESTING=False), unauthenticated requests to POST /transactions/ receive 401
        with self.settings(DEBUG=False, IS_TESTING=False):
            with patch("coordinator.views.get_user_from_request", return_value=None):
                resp = self.client.post(
                    "/transactions/",
                    data=json.dumps({"seat_ids": [1]}),
                    content_type="application/json",
                )
                self.assertEqual(resp.status_code, 401)
    def test_h10_hold_release_logging_with_correlation_id(self):
        """
        H10: Proves that inventory release failures during hold error handling
        are logged at WARNING level with correlation ID, rather than silently ignored.
        """
        cid = str(uuid.uuid4())
        mock_reserve_resp = MagicMock()
        mock_reserve_resp.status_code = 409
        mock_reserve_resp.json.return_value = {"error": "Seat already reserved", "code": "SEAT_UNAVAILABLE"}

        def side_effect(url, **kwargs):
            if "seats/pricing" in url:
                return MagicMock(status_code=200, json=lambda: [{"id": 1, "price": "100.00", "seat_number": "A1"}])
            if "seats/reserve" in url:
                return mock_reserve_resp
            if "seats/release" in url:
                raise requests.exceptions.ConnectionError("Inventory connection reset")
            return MagicMock(status_code=200, json=lambda: {})

        with patch("coordinator.views._http_post", side_effect=side_effect), \
             self.assertLogs("coordinator.views", level="WARNING") as cm:
            resp = self.client.post(
                "/transactions/",
                data=json.dumps({"seat_ids": [1], "action": "hold"}),
                content_type="application/json",
                HTTP_X_CORRELATION_ID=cid,
                HTTP_X_USER_ID="user_h10",
            )
            self.assertEqual(resp.status_code, 409)
            cid = resp.get("X-Correlation-ID")
            self.assertTrue(bool(cid))
            # Verify the warning log captured the correlation ID
            matching = [msg for msg in cm.output if cid in msg and "Failed to release hold" in msg]
            self.assertTrue(len(matching) >= 1, f"Expected warning log containing correlation ID {cid}, got: {cm.output}")













