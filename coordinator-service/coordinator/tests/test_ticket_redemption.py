from datetime import datetime, timedelta
from decimal import Decimal
import io
import uuid
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from coordinator.auth_views import _create_jwt
from coordinator.models import Transaction, Ticket


class TicketRedemptionAndIssuanceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user_id = "usr_ticket_tester"
        self.user_email = "ticket.shopper@gmail.com"
        self.jwt_token = _create_jwt(self.user_id, self.user_email, session_id="sid_ticket")
        self.auth_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.jwt_token}"}
        self.gate_key = "gate-secret-key-change-in-production"
        cache.clear()

        # Create COMPLETED transaction
        self.txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            user_id=self.user_id,
            user_email=self.user_email,
            status=Transaction.Status.COMPLETED,
            seat_ids=[101],
            quantity=1,
            amount=Decimal("150.00"),
        )

        # Create corresponding Ticket
        self.ticket = Ticket.objects.create(
            transaction=self.txn,
            ticket_code=f"DTC-{uuid.uuid4().hex[:10].upper()}",
            seat_id=101,
            seat_label="A-101",
            amount=Decimal("150.00"),
            event_name="Concert Live",
            venue="Main Arena",
            starts_at=timezone.now() + timedelta(days=7),
        )

    def tearDown(self):
        cache.clear()

    def test_authenticate_ticket_requester_security_rules(self):
        """Rejects ?token= query parameter, requires Bearer header, and returns 404 for non-owners."""
        url = f"/tickets/{self.txn.transaction_id}/"

        # 1. Prohibits query parameter auth
        resp = self.client.get(f"{url}?token={self.jwt_token}")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data.get("code"), "UNAUTHORIZED")

        # 2. Rejects request missing Authorization header
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 401)

        # 3. Non-owner receives 404 (anti-enumeration)
        other_jwt = _create_jwt("usr_intruder", "other@gmail.com")
        resp = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {other_jwt}")
        self.assertEqual(resp.status_code, 404)

        # 4. Malformed UUID receives 404
        resp = self.client.get("/tickets/not-a-uuid/", **self.auth_headers)
        self.assertEqual(resp.status_code, 404)

    def test_get_transaction_tickets_returns_qr_and_metadata(self):
        """Owner can retrieve tickets list with QR code and event metadata for COMPLETED transaction."""
        url = f"/tickets/{self.txn.transaction_id}/"
        resp = self.client.get(url, **self.auth_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["transaction_id"], str(self.txn.transaction_id))
        self.assertEqual(resp.data["event"]["name"], "Concert Live")
        self.assertEqual(len(resp.data["tickets"]), 1)

        ticket_data = resp.data["tickets"][0]
        self.assertEqual(ticket_data["seat_label"], "A-101")
        self.assertEqual(ticket_data["ticket_code"], self.ticket.ticket_code)
        self.assertFalse(ticket_data["is_used"])
        self.assertTrue(ticket_data["qr_code_base64"].startswith("data:image/png;base64,"))

    def test_ticket_issuance_fails_if_transaction_not_completed(self):
        """Attempting to access tickets for non-COMPLETED transaction returns 409 CONFLICT."""
        self.txn.status = Transaction.Status.HOLDING
        self.txn.save(update_fields=["status"])

        url = f"/tickets/{self.txn.transaction_id}/"
        resp = self.client.get(url, **self.auth_headers)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "CONFLICT")

    def test_get_ticket_pdf_and_ics_downloads(self):
        """PDF and ICS endpoints generate valid file downloads with correct MIME types and headers."""
        # PDF download
        pdf_url = f"/tickets/{self.txn.transaction_id}/pdf/"
        resp = self.client.get(pdf_url, **self.auth_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("attachment; filename=", resp["Content-Disposition"])
        self.assertTrue(resp.content.startswith(b"%PDF"))

        # ICS calendar download
        ics_url = f"/tickets/{self.txn.transaction_id}/calendar.ics"
        resp = self.client.get(ics_url, **self.auth_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/calendar; charset=utf-8")
        self.assertIn(b"BEGIN:VCALENDAR", resp.content)
        self.assertIn(b"END:VCALENDAR", resp.content)

    def test_public_verify_ticket_no_pii_and_status_checks(self):
        """Public verification endpoint verifies validity without exposing user PII."""
        # 1. Valid ticket (JSON format)
        url = f"/verify/{self.ticket.ticket_code}/"
        resp = self.client.get(f"{url}?format=json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), "VALID")
        self.assertEqual(resp.data.get("seat_label"), "A-101")
        self.assertNotIn("user_id", resp.data)
        self.assertNotIn("email", resp.data)
        self.assertNotIn("transaction_id", resp.data)

        # 2. Revoked ticket if transaction is not COMPLETED
        self.txn.status = Transaction.Status.COMPENSATED
        self.txn.save(update_fields=["status"])
        resp = self.client.get(f"{url}?format=json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), "REVOKED")

        # 3. Unknown ticket code returns 404
        resp = self.client.get("/verify/NON-EXISTENT-CODE/?format=json")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data.get("status"), "INVALID")

    def test_gate_redeem_authentication_and_key_verification(self):
        """Gate redeem endpoint requires valid GATE_API_KEY and supports key verification."""
        # Missing gate key
        resp = self.client.post("/gate/redeem/", {"ticket_code": self.ticket.ticket_code}, format="json")
        self.assertEqual(resp.status_code, 401)

        # Invalid gate key
        resp = self.client.post(
            "/gate/redeem/",
            {"ticket_code": self.ticket.ticket_code},
            HTTP_X_GATE_API_KEY="wrong-gate-key",
            format="json",
        )
        self.assertEqual(resp.status_code, 401)

        # Key verification action
        resp = self.client.post(
            "/gate/redeem/",
            {"action": "verify_key"},
            HTTP_X_GATE_API_KEY=self.gate_key,
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), "KEY_VALID")

    def test_gate_redeem_successful_and_prevents_double_entry(self):
        """First gate scan grants admission (200 REDEEMED); second scan fails with 409 ALREADY_USED."""
        headers = {"HTTP_X_GATE_API_KEY": self.gate_key}
        payload = {"ticket_code": self.ticket.ticket_code, "operator": "gate_officer_1"}

        # First scan: Admission granted
        resp = self.client.post("/gate/redeem/", payload, **headers, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), "REDEEMED")
        self.assertEqual(resp.data.get("seat_label"), "A-101")

        self.ticket.refresh_from_db()
        self.assertIsNotNone(self.ticket.used_at)
        self.assertEqual(self.ticket.used_by, "gate_officer_1")

        # Second scan: Double entry prevented
        resp_dup = self.client.post("/gate/redeem/", payload, **headers, format="json")
        self.assertEqual(resp_dup.status_code, 409)
        self.assertEqual(resp_dup.data.get("code"), "ALREADY_USED")
        self.assertIn("already been used", resp_dup.data.get("error"))

    def test_gate_redeem_refuses_uncompleted_transaction(self):
        """Gate redeem rejects tickets belonging to transactions that are not COMPLETED."""
        self.txn.status = Transaction.Status.PAYMENT_PROCESSING
        self.txn.save(update_fields=["status"])

        headers = {"HTTP_X_GATE_API_KEY": self.gate_key}
        resp = self.client.post(
            "/gate/redeem/",
            {"ticket_code": self.ticket.ticket_code},
            **headers,
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "TRANSACTION_NOT_COMPLETED")

    def test_send_ticket_email_dispatches_task_and_rate_limits(self):
        """send_ticket_email triggers background task and enforces rate limit of 3/hour."""
        url = f"/tickets/{self.txn.transaction_id}/email/"

        with patch("coordinator.tasks.send_ticket_email_task.delay") as mock_delay:
            resp = self.client.post(url, **self.auth_headers)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data.get("status"), "QUEUED")
            self.assertEqual(resp.data.get("recipient"), self.user_email)
            mock_delay.assert_called_once_with(str(self.txn.transaction_id))

        # Test rate limiting: 3 allowed per hour
        with patch("coordinator.tasks.send_ticket_email_task.delay"):
            # 2nd call
            resp2 = self.client.post(url, **self.auth_headers)
            self.assertEqual(resp2.status_code, 200)
            # 3rd call
            resp3 = self.client.post(url, **self.auth_headers)
            self.assertEqual(resp3.status_code, 200)
            # 4th call: Exceeds rate limit -> 429
            resp4 = self.client.post(url, **self.auth_headers)
            self.assertEqual(resp4.status_code, 429)
            self.assertEqual(resp4.data.get("code"), "RATE_LIMITED")
