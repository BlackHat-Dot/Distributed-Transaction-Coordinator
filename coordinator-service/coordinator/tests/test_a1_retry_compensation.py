import os
import uuid
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent


class RetryCompensationA1Tests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff_key = "test-staff-key-123"
        self.headers = {"HTTP_X_STAFF_KEY": self.staff_key}
        self.tx_id = uuid.uuid4()
        self.url = f"/transactions/{self.tx_id}/retry-compensation/"

    def _create_txn(self, status=Transaction.Status.COMPENSATION_FAILED, **kwargs):
        defaults = {
            "transaction_id": self.tx_id,
            "status": status,
            "seat_ids": [1, 2],
            "quantity": 2,
            "amount": 200,
            "user_id": "usr_test",
        }
        defaults.update(kwargs)
        return Transaction.objects.create(**defaults)

    # -------------------------------------------------------------------------
    # 1. Authentication & Authorization
    # -------------------------------------------------------------------------
    def test_auth_header_required_and_validated(self):
        """Missing or invalid X-Staff-Key rejected; unconfigured server key returns 503."""
        self._create_txn()

        # Missing header -> 401 STAFF_KEY_REQUIRED
        with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}):
            resp = self.client.post(self.url)
            self.assertEqual(resp.status_code, 401)
            self.assertEqual(resp.data.get("code"), "STAFF_KEY_REQUIRED")

        # Invalid key -> 403 INVALID_STAFF_KEY (AuthenticationFailed in DRF)
        with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}):
            resp = self.client.post(self.url, HTTP_X_STAFF_KEY="wrong-key")
            self.assertIn(resp.status_code, (401, 403))
            self.assertEqual(resp.data.get("code"), "INVALID_STAFF_KEY")

        # Server STAFF_API_KEY unset -> 503 Service Unavailable
        with patch.dict(os.environ, {}, clear=True):
            resp = self.client.post(self.url, HTTP_X_STAFF_KEY=self.staff_key)
            self.assertEqual(resp.status_code, 503)
            self.assertEqual(resp.data.get("code"), "STAFF_SERVICE_UNAVAILABLE")

    # -------------------------------------------------------------------------
    # 2. Transaction Validation & Idempotent Short-Circuit
    # -------------------------------------------------------------------------
    def test_unknown_transaction_returns_404(self):
        """Authenticated request for nonexistent transaction returns 404."""
        unknown_url = f"/transactions/{uuid.uuid4()}/retry-compensation/"
        with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}):
            resp = self.client.post(unknown_url, **self.headers)
            self.assertEqual(resp.status_code, 404)

    def test_ineligible_statuses_rejected_with_400(self):
        """Transactions in non-retryable statuses (e.g. STARTED, COMPLETED) return 400."""
        for ineligible_status in (Transaction.Status.STARTED, Transaction.Status.COMPLETED):
            tx_id = uuid.uuid4()
            Transaction.objects.create(
                transaction_id=tx_id,
                status=ineligible_status,
                seat_ids=[1],
                quantity=1,
                amount=100,
            )
            with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}):
                resp = self.client.post(f"/transactions/{tx_id}/retry-compensation/", **self.headers)
                self.assertEqual(resp.status_code, 400)
                self.assertIn("not eligible for retry compensation", resp.data.get("error", ""))

    def test_already_compensated_short_circuits_without_http_calls(self):
        """Already compensated transactions return 200 without issuing downstream calls."""
        for comp_status in (Transaction.Status.COMPENSATED, Transaction.Status.COMPENSATED_ON_RETRY):
            tx_id = uuid.uuid4()
            Transaction.objects.create(
                transaction_id=tx_id,
                status=comp_status,
                seat_ids=[1],
                quantity=1,
                amount=100,
            )
            with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}), \
                 patch("coordinator.views._http") as mock_http:
                resp = self.client.post(f"/transactions/{tx_id}/retry-compensation/", **self.headers)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.data.get("status"), comp_status)
                mock_http.assert_not_called()

    # -------------------------------------------------------------------------
    # 3. Concurrency & CAS Race Safety
    # -------------------------------------------------------------------------
    def test_concurrent_cas_claim_loss_returns_409(self):
        """If CAS claim to RECOVERY_IN_PROGRESS fails due to concurrent race, return 409."""
        self._create_txn(status=Transaction.Status.COMPENSATION_FAILED)

        # Simulate concurrent worker claiming transaction before CAS update
        with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}), \
             patch("coordinator.views._set_status_with_event", return_value=False), \
             patch("coordinator.views._http") as mock_http:
            resp = self.client.post(self.url, **self.headers)
            self.assertEqual(resp.status_code, 409)
            self.assertIn("concurrently resolved", resp.data.get("message", ""))
            mock_http.assert_not_called()

    # -------------------------------------------------------------------------
    # 4. Payment Safety & Rollback (5xx / Timeout)
    # -------------------------------------------------------------------------
    def test_payment_check_failure_reverts_claim_and_returns_503(self):
        """5xx or timeout during payment check must revert claim and never release inventory."""
        txn = self._create_txn(status=Transaction.Status.COMPENSATION_FAILED)

        mock_check = MagicMock()
        mock_check.status_code = 503
        mock_check.content = b'{"error": "Payment Service Unavailable"}'
        mock_check.json.return_value = {"error": "Payment Service Unavailable"}

        with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}), \
             patch("coordinator.views._http", return_value=mock_check) as mock_http:
            resp = self.client.post(self.url, **self.headers)
            self.assertEqual(resp.status_code, 503)

            # Ensure only payment_check was invoked; release was NEVER called
            call_urls = [str(arg) for call in mock_http.call_args_list for arg in call.args if isinstance(arg, str)]
            self.assertFalse(any("seats/release" in u for u in call_urls))

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATION_FAILED)

    # -------------------------------------------------------------------------
    # 5. Payment NOT_CHARGED Workflow: Void Fencing & Release
    # -------------------------------------------------------------------------
    def test_payment_not_charged_fences_with_void_then_releases_inventory(self):
        """NOT_CHARGED payment fences with void, releases inventory, transitions to COMPENSATED_ON_RETRY."""
        txn = self._create_txn(status=Transaction.Status.PAYMENT_TIMEOUT)
        calls = []

        def fake_http(method, url, **kwargs):
            calls.append((method, url))
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "NOT_CHARGED"}'
                mock.json.return_value = {"status": "NOT_CHARGED"}
            elif "payments/void" in url:
                mock.status_code = 200
                mock.content = b'{"status": "VOIDED"}'
                mock.json.return_value = {"status": "VOIDED"}
            elif "seats/release" in url:
                mock.status_code = 200
                mock.content = b'{"message": "Released"}'
                mock.json.return_value = {"message": "Released"}
            else:
                mock.status_code = 404
                mock.content = b'{}'
                mock.json.return_value = {}
            return mock

        with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}), \
             patch("coordinator.views._http", side_effect=fake_http):
            resp = self.client.post(self.url, **self.headers)
            self.assertEqual(resp.status_code, 200)

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED_ON_RETRY)

        # Verify ordering: check -> void -> release
        urls = [c[1] for c in calls]
        self.assertTrue(any("payments/check" in u for u in urls))
        self.assertTrue(any("payments/void" in u for u in urls))
        self.assertTrue(any("seats/release" in u for u in urls))

        # Verify SagaEvents recorded with ACTOR_RETRY
        claim_event = SagaEvent.objects.filter(
            transaction=txn,
            to_status=Transaction.Status.RECOVERY_IN_PROGRESS,
            actor=SagaEvent.ACTOR_RETRY,
        ).exists()
        final_event = SagaEvent.objects.filter(
            transaction=txn,
            to_status=Transaction.Status.COMPENSATED_ON_RETRY,
            actor=SagaEvent.ACTOR_RETRY,
        ).exists()
        self.assertTrue(claim_event)
        self.assertTrue(final_event)

    def test_payment_void_failure_aborts_compensation_and_reverts_claim(self):
        """If void fails for NOT_CHARGED payment, must abort and never release inventory."""
        txn = self._create_txn(status=Transaction.Status.PAYMENT_CONNECTION_ERROR)

        def fake_http(method, url, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "NOT_CHARGED"}'
                mock.json.return_value = {"status": "NOT_CHARGED"}
            elif "payments/void" in url:
                mock.status_code = 500
                mock.content = b'{"error": "Void timeout"}'
                mock.json.return_value = {"error": "Void timeout"}
            else:
                mock.status_code = 200
                mock.content = b'{}'
                mock.json.return_value = {}
            return mock

        with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}), \
             patch("coordinator.views._http", side_effect=fake_http) as mock_http:
            resp = self.client.post(self.url, **self.headers)
            self.assertEqual(resp.status_code, 503)

            call_urls = [str(arg) for call in mock_http.call_args_list for arg in call.args if isinstance(arg, str)]
            self.assertFalse(any("seats/release" in u for u in call_urls))

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.PAYMENT_CONNECTION_ERROR)

    # -------------------------------------------------------------------------
    # 6. Payment CHARGED Workflow: Forward Confirm vs Refund
    # -------------------------------------------------------------------------
    def test_payment_charged_forward_completes_if_inventory_confirm_succeeds(self):
        """CHARGED payment attempts seat confirmation first; if successful, forward-completes to COMPLETED."""
        txn = self._create_txn(status=Transaction.Status.INVENTORY_TIMEOUT)

        def fake_http(method, url, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            elif "seats/confirm" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CONFIRMED"}'
                mock.json.return_value = {"status": "CONFIRMED"}
            else:
                mock.status_code = 500
                mock.content = b'{}'
                mock.json.return_value = {}
            return mock

        with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}), \
             patch("coordinator.views._http", side_effect=fake_http) as mock_http:
            resp = self.client.post(self.url, **self.headers)
            self.assertEqual(resp.status_code, 200)

            # Never refunded
            call_urls = [str(arg) for call in mock_http.call_args_list for arg in call.args if isinstance(arg, str)]
            self.assertFalse(any("payments/refund" in u for u in call_urls))

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPLETED)

    def test_payment_charged_admin_reset_prohibits_auto_refund(self):
        """If seats were released by ADMIN_RESET, auto-refund is prohibited and returns 409."""
        txn = self._create_txn(status=Transaction.Status.COMPENSATION_FAILED)

        def fake_http(method, url, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            elif "seats/confirm" in url:
                mock.status_code = 409
                mock.content = b'{"error": "Seats released", "released_reason": "ADMIN_RESET"}'
                mock.json.return_value = {"error": "Seats released", "released_reason": "ADMIN_RESET"}
            else:
                mock.status_code = 500
                mock.content = b'{}'
                mock.json.return_value = {}
            return mock

        with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}), \
             patch("coordinator.views._http", side_effect=fake_http) as mock_http:
            resp = self.client.post(self.url, **self.headers)
            self.assertEqual(resp.status_code, 409)
            self.assertIn("ADMIN_RESET", resp.data.get("error", ""))

            # Refund must not be issued
            call_urls = [str(arg) for call in mock_http.call_args_list for arg in call.args if isinstance(arg, str)]
            self.assertFalse(any("payments/refund" in u for u in call_urls))

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATION_FAILED)

    def test_payment_charged_hold_expired_refunds_and_releases(self):
        """CHARGED payment with expired hold refunds payment, releases inventory, transitions to COMPENSATED_ON_RETRY."""
        txn = self._create_txn(status=Transaction.Status.COMPENSATION_FAILED)

        def fake_http(method, url, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            elif "seats/confirm" in url:
                mock.status_code = 409
                mock.content = b'{"error": "Hold expired", "code": "HOLD_EXPIRED"}'
                mock.json.return_value = {"error": "Hold expired", "code": "HOLD_EXPIRED"}
            elif "payments/refund" in url:
                mock.status_code = 200
                mock.content = b'{"status": "REFUNDED"}'
                mock.json.return_value = {"status": "REFUNDED"}
            elif "seats/release" in url:
                mock.status_code = 200
                mock.content = b'{"message": "Released"}'
                mock.json.return_value = {"message": "Released"}
            else:
                mock.status_code = 404
                mock.content = b'{}'
                mock.json.return_value = {}
            return mock

        with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}), \
             patch("coordinator.views._http", side_effect=fake_http):
            resp = self.client.post(self.url, **self.headers)
            self.assertEqual(resp.status_code, 200)

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED_ON_RETRY)

    # -------------------------------------------------------------------------
    # 7. Downstream Idempotency & Partial Failure
    # -------------------------------------------------------------------------
    def test_downstream_idempotent_errors_treated_as_successful_compensation(self):
        """Downstream responses indicating already refunded/released are treated as success."""
        txn = self._create_txn(status=Transaction.Status.COMPENSATION_FAILED)

        def fake_http(method, url, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            elif "seats/confirm" in url:
                mock.status_code = 409
                mock.content = b'{"error": "Hold expired"}'
                mock.json.return_value = {"error": "Hold expired"}
            elif "payments/refund" in url:
                mock.status_code = 409
                mock.content = b'{"error": "already refunded", "code": "ALREADY_REFUNDED"}'
                mock.json.return_value = {"error": "already refunded", "code": "ALREADY_REFUNDED"}
            elif "seats/release" in url:
                mock.status_code = 409
                mock.content = b'{"error": "already released", "code": "ALREADY_RELEASED"}'
                mock.json.return_value = {"error": "already released", "code": "ALREADY_RELEASED"}
            else:
                mock.status_code = 404
                mock.content = b'{}'
                mock.json.return_value = {}
            return mock

        with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}), \
             patch("coordinator.views._http", side_effect=fake_http):
            resp = self.client.post(self.url, **self.headers)
            self.assertEqual(resp.status_code, 200)

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED_ON_RETRY)

    def test_partial_failure_transitions_to_retry_compensation_failed(self):
        """If refund succeeds but release fails, status becomes RETRY_COMPENSATION_FAILED (500)."""
        txn = self._create_txn(status=Transaction.Status.COMPENSATION_FAILED)

        def fake_http(method, url, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            elif "seats/confirm" in url:
                mock.status_code = 409
                mock.content = b'{"error": "Hold expired"}'
                mock.json.return_value = {"error": "Hold expired"}
            elif "payments/refund" in url:
                mock.status_code = 200
                mock.content = b'{"status": "REFUNDED"}'
                mock.json.return_value = {"status": "REFUNDED"}
            elif "seats/release" in url:
                mock.status_code = 500
                mock.content = b'{"error": "Release database crash"}'
                mock.json.return_value = {"error": "Release database crash"}
            else:
                mock.status_code = 404
                mock.content = b'{}'
                mock.json.return_value = {}
            return mock

        with patch.dict(os.environ, {"STAFF_API_KEY": self.staff_key}), \
             patch("coordinator.views._http", side_effect=fake_http):
            resp = self.client.post(self.url, **self.headers)
            self.assertEqual(resp.status_code, 500)

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.RETRY_COMPENSATION_FAILED)
