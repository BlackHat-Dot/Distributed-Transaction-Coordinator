import uuid
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.test import TestCase, TransactionTestCase
from django.db import connection
from rest_framework.test import APIClient

from payments.models import Payment
from payments.views import NOT_CHARGED

DEFAULT_INTERNAL_KEY = "dtc-internal-coordinator-secret-key-2026"


class BasePaymentTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.credentials(HTTP_X_INTERNAL_SERVICE_KEY=DEFAULT_INTERNAL_KEY)


class ChargePaymentValidationTests(BasePaymentTestCase):
    def setUp(self):
        super().setUp()
        self.url = "/payments/charge/"

    def test_missing_transaction_id(self):
        resp = self.client.post(self.url, {"amount": 100}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("transaction_id", resp.data["error"])

    def test_empty_transaction_id(self):
        resp = self.client.post(
            self.url, {"transaction_id": "", "amount": 100}, format="json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_invalid_uuid_transaction_id(self):
        resp = self.client.post(
            self.url,
            {"transaction_id": "not-a-uuid", "amount": 100},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("valid UUID", resp.data["error"])

    def test_missing_amount(self):
        resp = self.client.post(
            self.url,
            {"transaction_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("amount", resp.data["error"])

    def test_zero_amount(self):
        resp = self.client.post(
            self.url,
            {"transaction_id": str(uuid.uuid4()), "amount": 0},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("greater than zero", resp.data["error"])

    def test_negative_amount(self):
        resp = self.client.post(
            self.url,
            {"transaction_id": str(uuid.uuid4()), "amount": -50},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("greater than zero", resp.data["error"])

    def test_non_numeric_amount(self):
        resp = self.client.post(
            self.url,
            {"transaction_id": str(uuid.uuid4()), "amount": "abc"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("valid number", resp.data["error"])

    def test_amount_with_more_than_two_decimal_places_rejected_with_400(self):
        tid = str(uuid.uuid4())
        for raw in ("19.999", "8.347", "25.125", 19.999):
            resp = self.client.post(
                self.url,
                {"transaction_id": tid, "amount": raw},
                format="json",
            )
            self.assertEqual(resp.status_code, 400)
            self.assertIn("more than 2 decimal places", resp.data["error"])

    def test_amount_exceeding_model_limit_rejected_with_400(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": "100000000.00"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("exceeds maximum allowable limit", resp.data["error"])

    def test_non_finite_amounts_rejected_with_400(self):
        tid = str(uuid.uuid4())
        for raw in ("NaN", "Infinity", "-Infinity", "sNaN"):
            resp = self.client.post(
                self.url,
                {"transaction_id": tid, "amount": raw},
                format="json",
            )
            self.assertEqual(resp.status_code, 400)
            self.assertIn("finite number", resp.data["error"])

    def test_non_mapping_body_rejected_with_400(self):
        resp = self.client.post(self.url, [1, 2, 3], format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("valid JSON object", resp.data["error"])

        resp_refund = self.client.post("/payments/refund/", [1, 2, 3], format="json")
        self.assertEqual(resp_refund.status_code, 400)
        self.assertIn("valid JSON object", resp_refund.data["error"])

    def test_user_id_zero_is_preserved_not_coerced_to_none(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 100, "user_id": 0},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["user_id"], "0")
        payment = Payment.objects.get(transaction_id=tid)
        self.assertEqual(payment.user_id, "0")

    def test_user_id_exceeding_max_length_rejected_with_400(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 100, "user_id": "u" * 65},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("user_id exceeds maximum length", resp.data["error"])

    def test_user_email_invalid_format_rejected_with_400(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 100, "user_email": "invalid-email-address"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("valid email address", resp.data["error"])

    def test_user_email_exceeding_max_length_rejected_with_400(self):
        tid = str(uuid.uuid4())
        too_long_email = ("a" * 250) + "@example.com"
        resp = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 100, "user_email": too_long_email},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("user_email exceeds maximum length", resp.data["error"])



class ChargePaymentSuccessTests(BasePaymentTestCase):
    def setUp(self):
        super().setUp()
        self.url = "/payments/charge/"

    def test_successful_charge(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 500},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["transaction_id"], tid)
        self.assertEqual(resp.data["amount"], "500.00")
        self.assertEqual(resp.data["status"], "CHARGED")

        payment = Payment.objects.get(transaction_id=tid)
        self.assertEqual(payment.status, Payment.Status.CHARGED)
        self.assertEqual(payment.amount, Decimal("500.00"))

    def test_decimal_amount_precision(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": "123.45"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["amount"], "123.45")

    def test_duplicate_transaction_id_is_idempotent(self):
        tid = str(uuid.uuid4())

        resp1 = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 500},
            format="json",
        )
        self.assertEqual(resp1.status_code, 201)

        resp2 = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 500},
            format="json",
        )
     
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data["status"], "CHARGED")
        self.assertEqual(resp2.data["amount"], "500.00")


        self.assertEqual(
            Payment.objects.filter(transaction_id=tid).count(), 1
        )

    def test_duplicate_with_different_amount_returns_409_conflict(self):
        tid = str(uuid.uuid4())

        self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 500},
            format="json",
        )
        resp2 = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 999},
            format="json",
        )
        self.assertEqual(resp2.status_code, 409)
        self.assertEqual(resp2.data.get("code"), "IDEMPOTENCY_PAYLOAD_MISMATCH")
        self.assertEqual(
            Payment.objects.filter(transaction_id=tid).count(), 1
        )

    def test_duplicate_with_different_user_id_returns_409_conflict(self):
        tid = str(uuid.uuid4())

        self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 500, "user_id": "alice"},
            format="json",
        )
        resp2 = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 500, "user_id": "bob"},
            format="json",
        )
        self.assertEqual(resp2.status_code, 409)
        self.assertEqual(resp2.data.get("code"), "IDEMPOTENCY_PAYLOAD_MISMATCH")

    def test_charge_replay_on_refunded_payment_returns_409(self):
        tid = str(uuid.uuid4())
        resp1 = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 500},
            format="json",
        )
        self.assertEqual(resp1.status_code, 201)

        refund_resp = self.client.post(
            "/payments/refund/",
            {"transaction_id": tid},
            format="json",
        )
        self.assertEqual(refund_resp.status_code, 200)

        # Replay charge on refunded transaction
        resp2 = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 500},
            format="json",
        )
        self.assertEqual(resp2.status_code, 409)
        self.assertIn("already refunded", resp2.data.get("error", "").lower())


class ChargePaymentIntentionalFailureTests(BasePaymentTestCase):
    def setUp(self):
        super().setUp()
        self.url = "/payments/charge/"

    def test_intentional_failure_returns_422(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 9999},
            format="json",
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.data["status"], NOT_CHARGED)
        self.assertIn("_testing", resp.data)

    def test_intentional_failure_creates_no_payment_row(self):
        tid = str(uuid.uuid4())
        self.client.post(
            self.url,
            {"transaction_id": tid, "amount": 9999},
            format="json",
        )
        self.assertFalse(Payment.objects.filter(transaction_id=tid).exists())

    def test_intentional_failure_still_validates_input_first(self):
        resp = self.client.post(
            self.url,
            {"transaction_id": "", "amount": 9999},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("transaction_id", resp.data["error"])

    def test_intentional_failure_disabled_in_production_processes_normally(self):
        tid = str(uuid.uuid4())
        with self.settings(ENABLE_TEST_PAYMENT_FAILURE=False, IS_TESTING=False):
            resp = self.client.post(
                self.url,
                {"transaction_id": tid, "amount": 9999},
                format="json",
            )
            self.assertEqual(resp.status_code, 201)
            self.assertEqual(resp.data["status"], "CHARGED")
            self.assertEqual(resp.data["amount"], "9999.00")

    def test_intentional_failure_does_not_leak_testing_hook_in_production(self):
        tid = str(uuid.uuid4())
        with self.settings(DEBUG=False, IS_TESTING=False, ENABLE_TEST_PAYMENT_FAILURE=True):
            resp = self.client.post(
                self.url,
                {"transaction_id": tid, "amount": 9999},
                format="json",
            )
            self.assertEqual(resp.status_code, 422)
            self.assertNotIn("_testing", resp.data)



class PaymentCheckTests(BasePaymentTestCase):
    def setUp(self):
        super().setUp()
        self.charge_url = "/payments/charge/"
        self.check_url = "/payments/check/"

    def test_check_charged_payment(self):
        tid = str(uuid.uuid4())
        self.client.post(
            self.charge_url,
            {"transaction_id": tid, "amount": 500},
            format="json",
        )

        resp = self.client.get(self.check_url, {"transaction_id": tid})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["transaction_id"], tid)
        self.assertEqual(resp.data["status"], "CHARGED")

    def test_check_not_charged_payment(self):
        tid = str(uuid.uuid4())
        resp = self.client.get(self.check_url, {"transaction_id": tid})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["transaction_id"], tid)
        self.assertEqual(resp.data["status"], NOT_CHARGED)

    def test_check_missing_transaction_id(self):
        resp = self.client.get(self.check_url)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("transaction_id", resp.data["error"])

    def test_check_invalid_uuid(self):
        resp = self.client.get(
            self.check_url, {"transaction_id": "invalid"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("valid UUID", resp.data["error"])

    def test_check_after_intentional_failure(self):
        tid = str(uuid.uuid4())
        self.client.post(
            self.charge_url,
            {"transaction_id": tid, "amount": 9999},
            format="json",
        )

        resp = self.client.get(self.check_url, {"transaction_id": tid})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], NOT_CHARGED)


def _is_postgres():
    return connection.vendor == "postgresql"


class ConcurrentChargeTests(TransactionTestCase):
    def test_concurrent_duplicate_charge(self):
        tid = str(uuid.uuid4())
        url = "/payments/charge/"
        num_threads = 5
        results = []

        def do_charge(_):
            client = APIClient()
            client.credentials(HTTP_X_INTERNAL_SERVICE_KEY=DEFAULT_INTERNAL_KEY)
            try:
                resp = client.post(
                    url,
                    {"transaction_id": tid, "amount": 250},
                    format="json",
                )
                return resp.status_code, resp.data
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [
                executor.submit(do_charge, i) for i in range(num_threads)
            ]
            for future in as_completed(futures):
                results.append(future.result())

        status_codes = [r[0] for r in results]
        for code in status_codes:
            self.assertIn(
                code,
                (200, 201),
                f"Unexpected status code {code} — "
                f"race condition produced an error",
            )

    
        self.assertEqual(
            status_codes.count(201),
            1,
            f"Expected exactly one 201 but got: {status_codes}",
        )

        for code, data in results:
            self.assertEqual(data["status"], "CHARGED")
            self.assertEqual(data["amount"], "250.00")

        self.assertEqual(
            Payment.objects.filter(transaction_id=tid).count(), 1
        )


class PaymentCorrelationIdMiddlewareTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_incoming_correlation_id_is_returned_in_response(self):
        cid = "pay-corr-id-" + str(uuid.uuid4())
        resp = self.client.get("/health/", HTTP_X_CORRELATION_ID=cid)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get("X-Correlation-ID"), cid)

    def test_missing_correlation_id_generates_uuid(self):
        resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 200)
        cid = resp.get("X-Correlation-ID")
        self.assertTrue(cid)
        uuid.UUID(cid)


class RefundPaymentTests(BasePaymentTestCase):
    def setUp(self):
        super().setUp()
        self.refund_url = "/payments/refund/"
        self.charge_url = "/payments/charge/"

    def test_refund_missing_transaction_id(self):
        resp = self.client.post(self.refund_url, {}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("transaction_id", resp.data.get("error", ""))

    def test_refund_invalid_uuid(self):
        resp = self.client.post(self.refund_url, {"transaction_id": "not-a-uuid"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("valid UUID", resp.data.get("error", ""))

    def test_refund_nonexistent_payment_returns_404(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(self.refund_url, {"transaction_id": tid}, format="json")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("Payment not found", resp.data.get("error", ""))

    def test_refund_charged_payment_succeeds(self):
        tid = str(uuid.uuid4())
        charge_resp = self.client.post(
            self.charge_url,
            {"transaction_id": tid, "amount": 500},
            format="json",
        )
        self.assertEqual(charge_resp.status_code, 201)

        refund_resp = self.client.post(
            self.refund_url,
            {"transaction_id": tid},
            format="json",
        )
        self.assertEqual(refund_resp.status_code, 200)
        self.assertEqual(refund_resp.data.get("status"), Payment.Status.REFUNDED)

        payment = Payment.objects.get(transaction_id=tid)
        self.assertEqual(payment.status, Payment.Status.REFUNDED)

    def test_refund_idempotent_multiple_calls_return_200(self):
        tid = str(uuid.uuid4())
        self.client.post(self.charge_url, {"transaction_id": tid, "amount": 500}, format="json")

        resp1 = self.client.post(self.refund_url, {"transaction_id": tid}, format="json")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.data.get("status"), Payment.Status.REFUNDED)

        resp2 = self.client.post(self.refund_url, {"transaction_id": tid}, format="json")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data.get("status"), Payment.Status.REFUNDED)

    def test_refund_non_charged_payment_returns_400(self):
        tid = str(uuid.uuid4())
        Payment.objects.create(
            transaction_id=tid,
            amount=500,
            status=Payment.Status.NOT_CHARGED,
        )

        resp = self.client.post(self.refund_url, {"transaction_id": tid}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Cannot refund", resp.data.get("error", ""))


class InternalServiceKeyAuthTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.valid_key = "dtc-internal-coordinator-secret-key-2026"
        self.invalid_key = "attacker-unauthorized-key"

    def test_missing_internal_service_key_rejected_with_403(self):
        tid = str(uuid.uuid4())
        # Even with DEBUG=True and IS_TESTING=True, missing header must be rejected
        with self.settings(DEBUG=True, IS_TESTING=True, INTERNAL_SERVICE_KEY=self.valid_key):
            resp = self.client.post("/payments/charge/", {"transaction_id": tid, "amount": 100}, format="json")
            self.assertEqual(resp.status_code, 403)
            self.assertIn("missing", resp.data.get("error", "").lower())

            resp = self.client.get(f"/payments/check/?transaction_id={tid}")
            self.assertEqual(resp.status_code, 403)

            resp = self.client.post("/payments/refund/", {"transaction_id": tid}, format="json")
            self.assertEqual(resp.status_code, 403)

    def test_empty_header_rejected_with_403(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            "/payments/charge/",
            {"transaction_id": tid, "amount": 100},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY="",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("missing", resp.data.get("error", "").lower())

    def test_whitespace_header_rejected_with_403(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            "/payments/charge/",
            {"transaction_id": tid, "amount": 100},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY="   ",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("missing", resp.data.get("error", "").lower())

    def test_non_ascii_header_rejected_without_500(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            "/payments/charge/",
            {"transaction_id": tid, "amount": 100},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY="secret-\u2603-unicode",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("invalid", resp.data.get("error", "").lower())

    def test_rejects_invalid_internal_service_key(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            "/payments/charge/",
            {"transaction_id": tid, "amount": 100},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY=self.invalid_key,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("invalid", resp.data.get("error", "").lower())

    def test_accepts_valid_internal_service_key(self):
        tid = str(uuid.uuid4())
        with self.settings(INTERNAL_SERVICE_KEY=self.valid_key):
            resp = self.client.post(
                "/payments/charge/",
                {"transaction_id": tid, "amount": 100},
                format="json",
                HTTP_X_INTERNAL_SERVICE_KEY=self.valid_key,
            )
            self.assertEqual(resp.status_code, 201)
            self.assertEqual(resp.data.get("status"), "CHARGED")

    def test_dynamic_key_update_at_call_time(self):
        tid = str(uuid.uuid4())
        new_key = "brand-new-rotated-secret-key-2026"
        with self.settings(INTERNAL_SERVICE_KEY=new_key):
            # Old key must be rejected now
            resp_old = self.client.post(
                "/payments/charge/",
                {"transaction_id": tid, "amount": 100},
                format="json",
                HTTP_X_INTERNAL_SERVICE_KEY=self.valid_key,
            )
            self.assertEqual(resp_old.status_code, 403)

            # New key must be accepted dynamically
            resp_new = self.client.post(
                "/payments/charge/",
                {"transaction_id": tid, "amount": 100},
                format="json",
                HTTP_X_INTERNAL_SERVICE_KEY=new_key,
            )
            self.assertEqual(resp_new.status_code, 201)

    def test_key_rotation_with_fallback(self):
        tid1 = str(uuid.uuid4())
        tid2 = str(uuid.uuid4())
        primary = "primary-key-alpha"
        fallback = "fallback-key-beta"
        with self.settings(INTERNAL_SERVICE_KEY=primary, INTERNAL_SERVICE_KEY_FALLBACK=fallback):
            # Primary key accepted
            resp1 = self.client.post(
                "/payments/charge/",
                {"transaction_id": tid1, "amount": 100},
                format="json",
                HTTP_X_INTERNAL_SERVICE_KEY=primary,
            )
            self.assertEqual(resp1.status_code, 201)

            # Fallback key accepted during rotation
            resp2 = self.client.post(
                "/payments/charge/",
                {"transaction_id": tid2, "amount": 100},
                format="json",
                HTTP_X_INTERNAL_SERVICE_KEY=fallback,
            )
            self.assertEqual(resp2.status_code, 201)

            # Random key rejected
            resp3 = self.client.post(
                "/payments/charge/",
                {"transaction_id": str(uuid.uuid4()), "amount": 100},
                format="json",
                HTTP_X_INTERNAL_SERVICE_KEY="some-other-key",
            )
            self.assertEqual(resp3.status_code, 403)

    def test_unconfigured_key_fails_closed(self):
        tid = str(uuid.uuid4())
        with self.settings(INTERNAL_SERVICE_KEY="", INTERNAL_SERVICE_KEY_FALLBACK=None):
            resp = self.client.post(
                "/payments/charge/",
                {"transaction_id": tid, "amount": 100},
                format="json",
                HTTP_X_INTERNAL_SERVICE_KEY="",
            )
            self.assertEqual(resp.status_code, 403)

    def test_health_check_unauthenticated(self):
        resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), "healthy")

    def test_app_config_ready_refuses_empty_key_when_not_testing(self):
        import sys
        from unittest import mock
        from django.apps import apps
        from django.core.exceptions import ImproperlyConfigured
        app_config = apps.get_app_config("payments")
        with mock.patch.object(sys, "argv", ["gunicorn", "payment_service.wsgi:application"]):
            with self.settings(IS_TESTING=False, INTERNAL_SERVICE_KEY=""):
                with self.assertRaises(ImproperlyConfigured):
                    app_config.ready()

    def test_app_config_ready_skips_key_check_for_non_serving_commands(self):
        import sys
        from unittest import mock
        from django.apps import apps
        app_config = apps.get_app_config("payments")
        with mock.patch.object(sys, "argv", ["manage.py", "migrate"]):
            with self.settings(IS_TESTING=False, INTERNAL_SERVICE_KEY=""):
                # Non-serving commands like migrate should not raise ImproperlyConfigured
                app_config.ready()


class VoidPaymentTests(BasePaymentTestCase):
    def setUp(self):
        super().setUp()
        self.url = "/payments/void/"

    def test_void_creates_tombstone(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(self.url, {"transaction_id": tid}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("transaction_id"), tid)
        self.assertEqual(resp.data.get("status"), "NOT_CHARGED")
        self.assertTrue(resp.data.get("voided"))

        payment = Payment.objects.get(transaction_id=tid)
        self.assertEqual(payment.status, Payment.Status.VOIDED)
        self.assertIsNone(payment.amount)

    def test_void_then_charge_returns_409(self):
        tid = str(uuid.uuid4())
        v_resp = self.client.post(self.url, {"transaction_id": tid}, format="json")
        self.assertEqual(v_resp.status_code, 200)

        c_resp = self.client.post(
            "/payments/charge/",
            {"transaction_id": tid, "amount": 100},
            format="json",
        )
        self.assertEqual(c_resp.status_code, 409)
        self.assertEqual(c_resp.data.get("code"), "TRANSACTION_VOIDED")
        self.assertEqual(c_resp.data.get("status"), "NOT_CHARGED")

    def test_charge_then_void_returns_charged_status(self):
        tid = str(uuid.uuid4())
        c_resp = self.client.post(
            "/payments/charge/",
            {"transaction_id": tid, "amount": "150.00"},
            format="json",
        )
        self.assertEqual(c_resp.status_code, 201)

        v_resp = self.client.post(self.url, {"transaction_id": tid}, format="json")
        self.assertEqual(v_resp.status_code, 200)
        self.assertEqual(v_resp.data.get("status"), "CHARGED")
        self.assertEqual(v_resp.data.get("amount"), "150.00")
        self.assertFalse(v_resp.data.get("voided", False))

        # Confirm DB still has CHARGED
        payment = Payment.objects.get(transaction_id=tid)
        self.assertEqual(payment.status, Payment.Status.CHARGED)
        self.assertEqual(payment.amount, Decimal("150.00"))

    def test_void_is_idempotent(self):
        tid = str(uuid.uuid4())
        resp1 = self.client.post(self.url, {"transaction_id": tid}, format="json")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.data.get("status"), "NOT_CHARGED")

        resp2 = self.client.post(self.url, {"transaction_id": tid}, format="json")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data.get("status"), "NOT_CHARGED")
        self.assertTrue(resp2.data.get("voided"))
        self.assertEqual(Payment.objects.filter(transaction_id=tid).count(), 1)

    def test_payment_check_returns_not_charged_and_voided_true(self):
        tid = str(uuid.uuid4())
        self.client.post(self.url, {"transaction_id": tid}, format="json")

        check_resp = self.client.get(f"/payments/check/?transaction_id={tid}")
        self.assertEqual(check_resp.status_code, 200)
        self.assertEqual(check_resp.data.get("status"), "NOT_CHARGED")
        self.assertTrue(check_resp.data.get("voided"))

    def test_charge_mismatch_payload_contract_a3(self):
        tid = str(uuid.uuid4())
        self.client.post(
            "/payments/charge/",
            {"transaction_id": tid, "amount": 100, "user_id": "usr_1"},
            format="json",
        )

        replay_resp = self.client.post(
            "/payments/charge/",
            {"transaction_id": tid, "amount": 200, "user_id": "usr_1"},
            format="json",
        )
        self.assertEqual(replay_resp.status_code, 409)
        self.assertEqual(replay_resp.data.get("code"), "IDEMPOTENCY_PAYLOAD_MISMATCH")
        self.assertEqual(replay_resp.data.get("existing_status"), "CHARGED")
        # Ensure contract: NO top-level 'status' key and NO 'payment' dict
        self.assertNotIn("status", replay_resp.data)
        self.assertNotIn("payment", replay_resp.data)

    def test_refund_on_voided_transaction_returns_400(self):
        tid = str(uuid.uuid4())
        self.client.post(self.url, {"transaction_id": tid}, format="json")

        resp = self.client.post("/payments/refund/", {"transaction_id": tid}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Cannot refund transaction with status VOIDED", resp.data.get("error"))

    def test_void_requires_auth(self):
        tid = str(uuid.uuid4())
        unauth_client = APIClient()
        resp = unauth_client.post(self.url, {"transaction_id": tid}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_void_requires_valid_body_and_transaction_id(self):
        resp1 = self.client.post(self.url, "not a mapping", content_type="application/json")
        self.assertEqual(resp1.status_code, 400)

        resp2 = self.client.post(self.url, {}, format="json")
        self.assertEqual(resp2.status_code, 400)

        resp3 = self.client.post(self.url, {"transaction_id": "not-a-uuid"}, format="json")
        self.assertEqual(resp3.status_code, 400)


class ConcurrentVoidChargeTests(TransactionTestCase):
    def tearDown(self):
        from django.db import connections
        connections.close_all()
        super().tearDown()

    def test_concurrent_void_vs_charge_race(self):
        tid = str(uuid.uuid4())

        def do_void():
            try:
                c = APIClient()
                c.credentials(HTTP_X_INTERNAL_SERVICE_KEY=DEFAULT_INTERNAL_KEY)
                return c.post("/payments/void/", {"transaction_id": tid}, format="json")
            finally:
                from django.db import connection
                connection.close()

        def do_charge():
            try:
                c = APIClient()
                c.credentials(HTTP_X_INTERNAL_SERVICE_KEY=DEFAULT_INTERNAL_KEY)
                return c.post("/payments/charge/", {"transaction_id": tid, "amount": 100}, format="json")
            finally:
                from django.db import connection
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            f_void = executor.submit(do_void)
            f_charge = executor.submit(do_charge)
            r_void = f_void.result()
            r_charge = f_charge.result()

        payment = Payment.objects.get(transaction_id=tid)
        if payment.status == Payment.Status.VOIDED:
            self.assertEqual(r_void.status_code, 200)
            self.assertEqual(r_void.data.get("status"), "NOT_CHARGED")
            self.assertEqual(r_charge.status_code, 409)
            self.assertEqual(r_charge.data.get("code"), "TRANSACTION_VOIDED")
        else:
            self.assertEqual(payment.status, Payment.Status.CHARGED)
            self.assertEqual(r_charge.status_code, 201)
            self.assertEqual(r_void.status_code, 200)
            self.assertEqual(r_void.data.get("status"), "CHARGED")




