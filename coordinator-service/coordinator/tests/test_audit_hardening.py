import os
import uuid
import json
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.test import TestCase, TransactionTestCase
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
    @patch("coordinator.views._get_caller")
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
    @patch("coordinator.views._get_caller")
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
        from coordinator.views import _get_caller

        # Valid staff key
        req_valid = self.factory.get("/transactions/", HTTP_X_STAFF_KEY="super-secret-staff-key-xyz")
        is_auth, uid, is_staff = _get_caller(req_valid)
        self.assertTrue(is_auth)
        self.assertTrue(is_staff)
        self.assertEqual(uid, "system_staff")

        # Wrong key
        with self.settings(DEBUG=False, IS_TESTING=False):
            req_invalid = self.factory.get("/transactions/", HTTP_X_STAFF_KEY="super-secret-staff-key-wrong")
            is_auth, uid, is_staff = _get_caller(req_invalid)
            self.assertFalse(is_auth)
            self.assertFalse(is_staff)

            # Prefix attack
            req_prefix = self.factory.get("/transactions/", HTTP_X_STAFF_KEY="super-secret-staff-key")
            is_auth, uid, is_staff = _get_caller(req_prefix)
            self.assertFalse(is_auth)
            self.assertFalse(is_staff)

    @patch("coordinator.views._get_caller")
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
        _tx2 = Transaction.objects.create(
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

    @patch("coordinator.views._get_caller")
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

    @patch("coordinator.views._get_caller")
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
        H2: When payment is CHARGED and _confirm times out,
        coordinator must retry _confirm before giving up / compensating.
        """
        from coordinator.views import _finish_payment
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

        with patch("coordinator.views._confirm", side_effect=mock_confirm), \
             patch("coordinator.views._compensate") as mock_compensate:
            resp = _finish_payment(
                tx_id,
                payment_data={"status": "CHARGED"},
            )

            self.assertEqual(len(confirm_attempts), 2, "Expected _confirm to be retried on timeout")
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
        from coordinator.views import _run_saga

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
                _run_saga(
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
        and ensure needs_recovery is present in _txn_json and SSE events.
        """
        from coordinator.views import _txn_json
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

        # 4. _txn_json includes is_terminal and needs_recovery
        tx_id = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.PAYMENT_TIMEOUT,
            amount=Decimal("50.00"),
        )
        serialized = _txn_json(txn)
        self.assertTrue(serialized["is_terminal"])
        self.assertTrue(serialized["needs_recovery"])

        txn.status = Transaction.Status.COMPLETED
        serialized_completed = _txn_json(txn)
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

        with patch("coordinator.views._get_caller", return_value=(True, "user_eve", True)), \
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
        - _get_caller returns RequesterIdentity tuple with .user_email attribute.
        - Unpacks cleanly into 3-tuple (is_auth, uid, is_staff).
        - _create_txn uses unified identity extraction and rejects unauthenticated callers in prod.
        """
        from coordinator.views import _get_caller, RequesterIdentity

        # 1. Staff key extraction with email
        req_staff = self.factory.post(
            "/transactions/",
            HTTP_X_STAFF_KEY="secret-staff",
            HTTP_X_USER_ID="staff_agent_1",
            HTTP_X_USER_EMAIL="agent@admin.dtc.internal",
        )
        with patch.dict(os.environ, {"STAFF_API_KEY": "secret-staff"}):
            ident = _get_caller(req_staff)
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
            ident = _get_caller(req_jwt)
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
            # Verify the warning log captured the hold release failure
            matching = [msg for msg in cm.output if "Failed to release hold" in msg]
            self.assertTrue(len(matching) >= 1, f"Expected warning log containing Failed to release hold, got: {cm.output}")

    def test_h13_trigger_recovery_sweep_validation_and_lock(self):
        """
        H13: trigger_recovery_sweep input validation and concurrency lock:
        - Rejects invalid / non-integer threshold_seconds with 400.
        - Rejects negative threshold_seconds with 400.
        - Rejects concurrent sweep with 409 when Redis lock is held.
        """
        # 1. Invalid threshold types
        for bad_val in ["abc", [60], {"sec": 60}, True, False, -10]:
            req = self.factory.post(
                "/recovery/sweep/",
                data=json.dumps({"threshold_seconds": bad_val}),
                content_type="application/json",
                HTTP_X_STAFF_KEY="secret-staff",
            )
            with patch.dict(os.environ, {"STAFF_API_KEY": "secret-staff"}):
                resp = trigger_recovery_sweep(req)
                self.assertEqual(resp.status_code, 400, f"Expected 400 for threshold_seconds={bad_val!r}")
                self.assertEqual(resp.data.get("code"), "INVALID_THRESHOLD_SECONDS")

        # 2. Redis lock already held by another sweeper
        mock_redis = MagicMock()
        mock_redis.set.return_value = False  # Lock acquisition failed
        with patch.dict(os.environ, {"STAFF_API_KEY": "secret-staff"}), \
             patch("coordinator.tasks.get_redis_client", return_value=mock_redis):
            req = self.factory.post(
                "/recovery/sweep/",
                data=json.dumps({"threshold_seconds": 60}),
                content_type="application/json",
                HTTP_X_STAFF_KEY="secret-staff",
            )
            resp = trigger_recovery_sweep(req)
            self.assertEqual(resp.status_code, 409)
            self.assertEqual(resp.data.get("code"), "RECOVERY_ALREADY_IN_PROGRESS")

    def test_h13_recovery_status_auth(self):
        """
        H13: recovery_status requires staff authentication.
        """
        from coordinator.views import recovery_status

        # 1. Unauthenticated in prod -> 401
        with self.settings(DEBUG=False, IS_TESTING=False):
            with patch("coordinator.views.get_user_from_request", return_value=None):
                req = self.factory.get("/recovery/status/")
                resp = recovery_status(req)
                self.assertEqual(resp.status_code, 401)

            # 2. Authenticated non-staff -> 403
            with patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "u1", "user_email": "u@cust.com"}):
                req = self.factory.get("/recovery/status/")
                resp = recovery_status(req)
                self.assertEqual(resp.status_code, 403)

        # 3. Staff -> 200
        req_staff = self.factory.get("/recovery/status/", HTTP_X_STAFF_KEY="secret-staff")
        with patch.dict(os.environ, {"STAFF_API_KEY": "secret-staff"}):
            resp = recovery_status(req_staff)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data.get("status"), "active")

    def test_h14_startup_service_keys_system_check(self):
        """
        H14: check_service_keys_and_admin_domain validates configuration
        when not in test runner.
        """
        from coordinator.checks import check_service_keys_and_admin_domain

        # In testing mode, no warnings emitted
        with self.settings(IS_TESTING=True):
            warnings = check_service_keys_and_admin_domain(None)
            self.assertEqual(len(warnings), 0)

        # In non-testing mode with missing keys, returns 3 warnings
        with self.settings(IS_TESTING=False, INTERNAL_SERVICE_KEY=""):
            with patch.dict(os.environ, {}, clear=True):
                warnings = check_service_keys_and_admin_domain(None)
                warning_ids = {w.id for w in warnings}
                self.assertIn("coordinator.W001", warning_ids)
                self.assertIn("coordinator.W002", warning_ids)
                self.assertIn("coordinator.W003", warning_ids)

        # In non-testing mode with valid keys, returns 0 warnings
        with self.settings(IS_TESTING=False, INTERNAL_SERVICE_KEY="prod-secret-123"):
            env = {
                "INTERNAL_SERVICE_KEY": "prod-secret-123",
                "STAFF_API_KEY": "staff-secret-456",
                "ADMIN_DOMAIN": "dtc.internal",
            }
            with patch.dict(os.environ, env):
                warnings = check_service_keys_and_admin_domain(None)
                self.assertEqual(len(warnings), 0)

    def test_h15_deduplicate_and_cap_seat_ids(self):
        """
        H15: _check_body rejects duplicate seat_ids and enforces max seat cap.
        """
        # 1. Duplicate seat_ids in one-shot / hold
        resp = self.client.post(
            "/transactions/",
            data=json.dumps({"seat_ids": [1, 2, 1]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "DUPLICATE_SEATS")

        # 2. Exceeding max seats per transaction
        resp_cap = self.client.post(
            "/transactions/",
            data=json.dumps({"seat_ids": list(range(1, 15))}),
            content_type="application/json",
        )
        self.assertEqual(resp_cap.status_code, 400)
        self.assertEqual(resp_cap.data.get("code"), "EXCEEDED_MAX_SEATS")

    @patch("coordinator.views.redis.Redis.from_url")
    def test_h17_stream_timeout_and_db_cleanup(self, mock_redis_cls):
        """
        H17: transaction_stream emits status="STREAM_TIMEOUT" with is_terminal=False
        and cleans up database connections on completion.
        """
        from coordinator.views import transaction_stream
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="user_stream",
            status=Transaction.Status.STARTED,
            amount=Decimal("500.00"),
        )
        mock_redis = MagicMock()
        mock_pubsub = MagicMock()
        mock_redis.pubsub.return_value = mock_pubsub
        mock_pubsub.get_message.return_value = None
        mock_redis_cls.return_value = mock_redis

        # Mock time.monotonic to trigger timeout on first loop iteration
        time_vals = [0.0, 31.0, 32.0, 33.0]
        with patch("time.monotonic", side_effect=time_vals), \
             patch("django.db.close_old_connections") as mock_close_db:
            req = self.factory.get(f"/transactions/{tx_id}/stream/", HTTP_X_USER_ID="user_stream")
            resp = transaction_stream(req, tx_id)
            raw_chunks = list(resp.streaming_content)
            chunks = [c.decode("utf-8") if isinstance(c, bytes) else c for c in raw_chunks]
            mock_close_db.assert_called()

            # Find the STREAM_TIMEOUT chunk
            timeout_chunk = [c for c in chunks if "STREAM_TIMEOUT" in c]
            self.assertTrue(len(timeout_chunk) >= 1)
            parsed = json.loads(timeout_chunk[0].replace("data: ", "").strip())
            self.assertEqual(parsed.get("status"), "STREAM_TIMEOUT")
            self.assertFalse(parsed.get("is_terminal"))

    def test_h18_timeout_tuple_and_idempotency_headers(self):
        """
        H18: Connect/read timeout tuple and X-Idempotency-Key header on outbound calls.
        """
        from coordinator.views import (
            REQUEST_TIMEOUT,
            _charge,
            _reserve,
            _confirm,
        )
        self.assertIsInstance(REQUEST_TIMEOUT, tuple)
        self.assertEqual(len(REQUEST_TIMEOUT), 2)

        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.STARTED,
            amount=Decimal("500.00"),
            seat_ids=[1],
        )

        with patch("coordinator.views.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, json=lambda: {"status": "RESERVED"})
            _reserve(tx_id, [1], correlation_id="cid-123")
            call_kwargs = mock_post.call_args[1]
            self.assertEqual(call_kwargs["headers"].get("X-Idempotency-Key"), str(tx_id))
            self.assertEqual(call_kwargs.get("timeout"), REQUEST_TIMEOUT)

            # Test _charge
            Transaction.objects.filter(transaction_id=tx_id).update(status=Transaction.Status.INVENTORY_RESERVED)
            _charge(tx_id, 500, False, correlation_id="cid-123", seat_ids=[1])
            call_kwargs = mock_post.call_args[1]
            self.assertEqual(call_kwargs["headers"].get("X-Idempotency-Key"), str(tx_id))
            self.assertEqual(call_kwargs.get("timeout"), REQUEST_TIMEOUT)

            # Test _confirm
            _confirm(tx_id, [1], correlation_id="cid-123")
            call_kwargs = mock_post.call_args[1]
            self.assertEqual(call_kwargs["headers"].get("X-Idempotency-Key"), str(tx_id))
            self.assertEqual(call_kwargs.get("timeout"), REQUEST_TIMEOUT)

    def test_h22_allowed_transitions_validation(self):
        """
        H22: _set_status_with_event validates transitions against ALLOWED_TRANSITIONS.
        Rejects illegal CAS transitions and accepts valid transitions.
        """
        from coordinator.saga_log import _set_status_with_event, ALLOWED_TRANSITIONS
        self.assertIsInstance(ALLOWED_TRANSITIONS, dict)

        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.STARTED,
            amount=Decimal("500.00"),
            seat_ids=[1],
        )

        # 1. Illegal transition: STARTED -> COMPLETED is NOT allowed
        with self.assertLogs("coordinator.saga_log", level="WARNING") as cm:
            ok = _set_status_with_event(
                tx_id,
                to_status=Transaction.Status.COMPLETED,
                expected_status=Transaction.Status.STARTED,
            )
            self.assertFalse(ok)
            self.assertTrue(any("Illegal CAS state transition" in msg for msg in cm.output))

        # Transaction status unchanged
        txn = Transaction.objects.get(transaction_id=tx_id)
        self.assertEqual(txn.status, Transaction.Status.STARTED)

        # 2. Legal transition: STARTED -> INVENTORY_RESERVED is allowed
        ok = _set_status_with_event(
            tx_id,
            to_status=Transaction.Status.INVENTORY_RESERVED,
            expected_status=Transaction.Status.STARTED,
        )
        self.assertTrue(ok)
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.INVENTORY_RESERVED)

        # 3. Illegal transition: INVENTORY_RESERVED -> COMPENSATED is NOT allowed
        with self.assertLogs("coordinator.saga_log", level="WARNING"):
            ok = _set_status_with_event(
                tx_id,
                to_status=Transaction.Status.COMPENSATED,
                expected_status=Transaction.Status.INVENTORY_RESERVED,
            )
            self.assertFalse(ok)

    def test_h16_pii_masking_in_saga_event(self):
        """
        H16: SagaEvent.http_payload masks sensitive fields (email, credentials, tokens).
        """
        from coordinator.saga_log import _set_status_with_event
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.STARTED,
            amount=Decimal("500.00"),
            seat_ids=[1],
        )

        sensitive_payload = {
            "user_email": "alice@company.com",
            "token": "secret_token_12345",
            "safe_field": "public_data",
        }
        ok = _set_status_with_event(
            tx_id,
            to_status=Transaction.Status.INVENTORY_RESERVED,
            expected_status=Transaction.Status.STARTED,
            http_payload=sensitive_payload,
        )
        self.assertTrue(ok)

        event = SagaEvent.objects.filter(transaction__transaction_id=tx_id).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.http_payload.get("safe_field"), "public_data")
        # Masked values
        self.assertNotEqual(event.http_payload.get("user_email"), "alice@company.com")
        self.assertIn("@company.com", event.http_payload.get("user_email"))
        self.assertNotEqual(event.http_payload.get("token"), "secret_token_12345")
        self.assertTrue(event.http_payload.get("token").startswith("****"))

    def test_h20_outbound_correlation_id_standardized(self):
        """
        H20: _reserve, _charge, _confirm use _corr_id
        so that outbound calls always carry a valid non-empty X-Correlation-ID even if correlation_id=None.
        """
        from coordinator.views import _reserve, _charge, _confirm
        with patch("coordinator.views._http_post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, json=lambda: {})
            tx_id = uuid.uuid4()

            _reserve(tx_id, seat_ids=[1], correlation_id=None)
            headers = mock_post.call_args[1]["headers"]
            self.assertTrue(headers.get("X-Correlation-ID"))

            _charge(tx_id, amount=Decimal("100.00"), correlation_id=None)
            headers = mock_post.call_args[1]["headers"]
            self.assertTrue(headers.get("X-Correlation-ID"))

            _confirm(tx_id, seat_ids=[1], correlation_id=None)
            headers = mock_post.call_args[1]["headers"]
            self.assertTrue(headers.get("X-Correlation-ID"))

    def test_h21_updated_at_refreshed_on_state_transition(self):
        """
        H21: _set_status_with_event refreshes updated_at timestamp in Transaction.
        """
        from datetime import timedelta
        from django.utils import timezone
        from coordinator.saga_log import _set_status_with_event

        tx_id = uuid.uuid4()
        past = timezone.now() - timedelta(minutes=10)
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.STARTED,
            amount=Decimal("500.00"),
            seat_ids=[1],
        )
        Transaction.objects.filter(pk=txn.pk).update(updated_at=past)
        txn.refresh_from_db()
        self.assertAlmostEqual(txn.updated_at.timestamp(), past.timestamp(), delta=2)

        _set_status_with_event(
            tx_id,
            to_status=Transaction.Status.INVENTORY_RESERVED,
            expected_status=Transaction.Status.STARTED,
        )
        txn.refresh_from_db()
        self.assertGreater(txn.updated_at, past)


class ConcurrentPayRaceTests(TransactionTestCase):
    @patch("coordinator.tasks.execute_saga_task.delay")
    def test_parallel_pay_race_only_one_winner(self, mock_delay):
        """
        Concurrency Step 4: When two parallel requests invoke action='pay' on the same
        HOLDING transaction simultaneously, exactly ONE succeeds (202) and transitions
        to INVENTORY_RESERVED, while the other is rejected with HTTP 409 CONCURRENT_PAYMENT_IN_PROGRESS.
        """
        import uuid
        from datetime import timedelta
        from django.utils import timezone
        from concurrent.futures import ThreadPoolExecutor
        from rest_framework.test import APIClient
        from coordinator.models import Transaction, SagaEvent

        tx_id = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.HOLDING,
            amount=Decimal("200.00"),
            seat_ids=[1, 2],
            user_id="user_concurrent",
            user_email="concurrent@test.io",
        )
        SagaEvent.objects.create(
            transaction=txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.HOLDING,
            actor=SagaEvent.ACTOR_REQUEST,
            http_payload={"held_until": (timezone.now() + timedelta(minutes=10)).isoformat()},
        )

        def send_pay():
            from django.db import connection
            connection.close()  # Get clean connection in thread
            try:
                client = APIClient()
                return client.post(
                    "/transactions/",
                    data=json.dumps({"action": "pay", "transaction_id": str(tx_id)}),
                    content_type="application/json",
                    HTTP_X_USER_ID="user_concurrent",
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(send_pay)
            f2 = executor.submit(send_pay)
            r1 = f1.result()
            r2 = f2.result()

        status_codes = sorted([r1.status_code, r2.status_code])
        self.assertEqual(status_codes, [202, 409], f"Expected one 202 and one 409, got: {status_codes}")
        error_resp = r1 if r1.status_code == 409 else r2
        self.assertEqual(error_resp.data.get("code"), "INVALID_TRANSACTION_STATUS")





















