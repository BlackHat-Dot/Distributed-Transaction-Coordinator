from decimal import Decimal
import uuid

from django.conf import settings
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from payments.models import Payment
from payments.views import TESTING_FAILURE_AMOUNT


class PaymentOperationsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.internal_key = getattr(settings, "INTERNAL_SERVICE_KEY", "test-internal-key")
        self.headers = {"HTTP_X_INTERNAL_SERVICE_KEY": self.internal_key}
        self.tx_id = uuid.uuid4()
        self.user_id = "usr_pay_tester"
        self.user_email = "shopper@gmail.com"
        self.amount = Decimal("150.00")

    def test_charge_payment_success_creates_charged_record(self):
        """Valid charge creates a Payment record with status CHARGED and returns 201."""
        payload = {
            "transaction_id": str(self.tx_id),
            "amount": str(self.amount),
            "user_id": self.user_id,
            "user_email": self.user_email,
        }
        resp = self.client.post("/payments/charge/", payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["transaction_id"], str(self.tx_id))
        self.assertEqual(resp.data["status"], Payment.Status.CHARGED)
        self.assertEqual(resp.data["amount"], "150.00")

        # Verify DB
        payment = Payment.objects.get(transaction_id=self.tx_id)
        self.assertEqual(payment.status, Payment.Status.CHARGED)
        self.assertEqual(payment.amount, self.amount)
        self.assertEqual(payment.user_id, self.user_id)
        self.assertEqual(payment.user_email, self.user_email)

    def test_charge_payment_idempotent_replay(self):
        """Identical charge payload returns existing CHARGED payment with 200 OK."""
        payment = Payment.objects.create(
            transaction_id=self.tx_id,
            user_id=self.user_id,
            user_email=self.user_email,
            amount=self.amount,
            status=Payment.Status.CHARGED,
        )

        payload = {
            "transaction_id": str(self.tx_id),
            "amount": str(self.amount),
            "user_id": self.user_id,
            "user_email": self.user_email,
        }
        resp = self.client.post("/payments/charge/", payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["transaction_id"], str(self.tx_id))
        self.assertEqual(resp.data["status"], Payment.Status.CHARGED)

    def test_charge_payment_idempotency_mismatch_conflict(self):
        """Replay of transaction ID with different amount or user returns 409 IDEMPOTENCY_PAYLOAD_MISMATCH."""
        Payment.objects.create(
            transaction_id=self.tx_id,
            user_id=self.user_id,
            user_email=self.user_email,
            amount=self.amount,
            status=Payment.Status.CHARGED,
        )

        # Mismatched amount
        payload = {
            "transaction_id": str(self.tx_id),
            "amount": "200.00",
            "user_id": self.user_id,
            "user_email": self.user_email,
        }
        resp = self.client.post("/payments/charge/", payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "IDEMPOTENCY_PAYLOAD_MISMATCH")

        # Mismatched user_id
        payload_user = {
            "transaction_id": str(self.tx_id),
            "amount": str(self.amount),
            "user_id": "usr_different",
            "user_email": self.user_email,
        }
        resp_user = self.client.post("/payments/charge/", payload_user, format="json", **self.headers)
        self.assertEqual(resp_user.status_code, 409)
        self.assertEqual(resp_user.data.get("code"), "IDEMPOTENCY_PAYLOAD_MISMATCH")

    def test_charge_payment_voided_or_refunded_rejected(self):
        """Charging a VOIDED transaction returns 409; charging a REFUNDED transaction returns 409."""
        # 1. Voided transaction
        Payment.objects.create(
            transaction_id=self.tx_id,
            status=Payment.Status.VOIDED,
            amount=None,
        )
        payload = {
            "transaction_id": str(self.tx_id),
            "amount": str(self.amount),
            "user_id": self.user_id,
        }
        resp = self.client.post("/payments/charge/", payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "TRANSACTION_VOIDED")

        # 2. Refunded transaction
        other_tx = uuid.uuid4()
        Payment.objects.create(
            transaction_id=other_tx,
            status=Payment.Status.REFUNDED,
            amount=self.amount,
            user_id=self.user_id,
        )
        payload2 = {
            "transaction_id": str(other_tx),
            "amount": str(self.amount),
            "user_id": self.user_id,
        }
        resp2 = self.client.post("/payments/charge/", payload2, format="json", **self.headers)
        self.assertEqual(resp2.status_code, 409)

    def test_charge_payment_simulated_decline(self):
        """Amount equal to TESTING_FAILURE_AMOUNT simulates gateway decline with 422 NOT_CHARGED."""
        payload = {
            "transaction_id": str(self.tx_id),
            "amount": str(TESTING_FAILURE_AMOUNT),
            "user_id": self.user_id,
        }
        with self.settings(ENABLE_TEST_PAYMENT_FAILURE=True):
            resp = self.client.post("/payments/charge/", payload, format="json", **self.headers)
            self.assertEqual(resp.status_code, 422)
            self.assertEqual(resp.data.get("status"), Payment.Status.NOT_CHARGED)
            self.assertEqual(resp.data.get("error"), "Payment declined")

        # Database must not record a charge
        self.assertFalse(Payment.objects.filter(transaction_id=self.tx_id).exists())

    def test_charge_payment_validation_rules(self):
        """Validates positive amounts, decimal precision <= 2, and valid UUID format."""
        # Negative amount
        resp = self.client.post(
            "/payments/charge/",
            {"transaction_id": str(self.tx_id), "amount": "-10.00"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 400)

        # > 2 decimal places rejected (no silent rounding)
        resp = self.client.post(
            "/payments/charge/",
            {"transaction_id": str(self.tx_id), "amount": "10.555"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("more than 2 decimal places", resp.data.get("error"))

        # Invalid UUID
        resp = self.client.post(
            "/payments/charge/",
            {"transaction_id": "invalid-uuid", "amount": "50.00"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 400)

    def test_payment_check_query_realtime_state(self):
        """GET /payments/check/ returns current payment state without side effects."""
        # 1. Uncharged
        resp = self.client.get(f"/payments/check/?transaction_id={self.tx_id}", **self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), Payment.Status.NOT_CHARGED)

        # 2. Charged
        Payment.objects.create(
            transaction_id=self.tx_id,
            status=Payment.Status.CHARGED,
            amount=self.amount,
            user_id=self.user_id,
        )
        resp = self.client.get(f"/payments/check/?transaction_id={self.tx_id}", **self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), Payment.Status.CHARGED)
        self.assertEqual(resp.data.get("amount"), "150.00")

        # 3. Voided
        void_tx = uuid.uuid4()
        Payment.objects.create(
            transaction_id=void_tx,
            status=Payment.Status.VOIDED,
            amount=None,
        )
        resp = self.client.get(f"/payments/check/?transaction_id={void_tx}", **self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), Payment.Status.NOT_CHARGED)
        self.assertTrue(resp.data.get("voided"))

    def test_void_payment_creates_tombstone_and_is_idempotent(self):
        """Voiding an uncharged transaction creates a VOIDED tombstone, fences future charges."""
        resp = self.client.post(
            "/payments/void/",
            {"transaction_id": str(self.tx_id)},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), Payment.Status.NOT_CHARGED)
        self.assertTrue(resp.data.get("voided"))

        payment = Payment.objects.get(transaction_id=self.tx_id)
        self.assertEqual(payment.status, Payment.Status.VOIDED)
        self.assertIsNone(payment.amount)

        # Idempotent replay
        resp2 = self.client.post(
            "/payments/void/",
            {"transaction_id": str(self.tx_id)},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(resp2.data.get("voided"))

    def test_refund_payment_success_and_idempotency(self):
        """Refunding a CHARGED payment marks status REFUNDED; replays are idempotent."""
        Payment.objects.create(
            transaction_id=self.tx_id,
            status=Payment.Status.CHARGED,
            amount=self.amount,
            user_id=self.user_id,
        )

        resp = self.client.post(
            "/payments/refund/",
            {"transaction_id": str(self.tx_id)},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), Payment.Status.REFUNDED)

        payment = Payment.objects.get(transaction_id=self.tx_id)
        self.assertEqual(payment.status, Payment.Status.REFUNDED)
        self.assertIsNotNone(payment.refunded_at)

        # Idempotent replay
        resp2 = self.client.post(
            "/payments/refund/",
            {"transaction_id": str(self.tx_id)},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data.get("status"), Payment.Status.REFUNDED)

        # Refunding non-existent payment returns 404
        resp_404 = self.client.post(
            "/payments/refund/",
            {"transaction_id": str(uuid.uuid4())},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp_404.status_code, 404)

    def test_payment_authentication_requires_internal_service_key(self):
        """All payment-modifying endpoints reject requests with missing or invalid service keys (403)."""
        # Missing key
        resp = self.client.post(
            "/payments/charge/",
            {"transaction_id": str(self.tx_id), "amount": "50.00"},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

        # Invalid key
        resp_bad = self.client.post(
            "/payments/charge/",
            {"transaction_id": str(self.tx_id), "amount": "50.00"},
            HTTP_X_INTERNAL_SERVICE_KEY="wrong-secret-key",
            format="json",
        )
        self.assertEqual(resp_bad.status_code, 403)
