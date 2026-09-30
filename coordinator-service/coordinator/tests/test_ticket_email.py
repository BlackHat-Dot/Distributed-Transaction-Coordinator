import time
import uuid
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.conf import settings
from django.core.cache import cache
from django.test import TransactionTestCase
from django.utils import timezone
import jwt
from rest_framework.test import APIClient

from coordinator.models import Transaction, Ticket
from coordinator.tasks import send_ticket_email_task


class TicketEmailTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        self.user_id = "usr_alice"
        self.user_email = "alice@example.com"
        self.secret_key = settings.SECRET_KEY

        cache.clear()

        self.txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            user_id=self.user_id,
            user_email=self.user_email,
            status=Transaction.Status.COMPLETED,
            seat_ids=[1],
            seat_numbers="A1",
            amount=Decimal("500.00"),
            quantity=1,
        )

        import secrets
        self.ticket = Ticket.objects.create(
            transaction=self.txn,
            seat_id=1,
            seat_label="A1",
            event_name="Coldplay: Music of the Spheres World Tour",
            venue="Grand Arena Stadium",
            starts_at=timezone.now() + timezone.timedelta(days=14),
            amount=Decimal("500.00"),
            ticket_code=secrets.token_urlsafe(16),
        )


    def _generate_jwt(self, user_id, email="alice@example.com"):
        payload = {
            "user_id": user_id,
            "email": email,
            "exp": time.time() + 3600,
            "iat": time.time(),
        }
        return jwt.encode(payload, self.secret_key, algorithm="HS256")

    @patch("coordinator.tasks.send_ticket_email_task.delay")
    def test_email_endpoint_dispatches_task_to_owner_stored_email(self, mock_delay):
        """Owner calling email endpoint queues Celery task to send PDF to stored email only."""
        token = self._generate_jwt(self.user_id, self.user_email)
        resp = self.client.post(
            f"/tickets/{self.txn.transaction_id}/email/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), "QUEUED")
        self.assertEqual(resp.data.get("recipient"), self.user_email)
        mock_delay.assert_called_once_with(str(self.txn.transaction_id))

    @patch("coordinator.tasks.send_ticket_email_task.delay")
    def test_email_endpoint_ignores_custom_recipient_parameter(self, mock_delay):
        """Email endpoint accepts NO recipient parameter from caller and sends only to owner email."""
        token = self._generate_jwt(self.user_id, self.user_email)
        resp = self.client.post(
            f"/tickets/{self.txn.transaction_id}/email/",
            {"to": "attacker@evil.com", "recipient": "attacker@evil.com", "email": "attacker@evil.com"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("recipient"), self.user_email)
        self.assertNotEqual(resp.data.get("recipient"), "attacker@evil.com")
        mock_delay.assert_called_once_with(str(self.txn.transaction_id))

    @patch("coordinator.tasks.send_ticket_email_task.delay")
    def test_email_endpoint_rate_limited_to_3_per_hour(self, mock_delay):
        """Rate limited to 3 email dispatches per hour per transaction; 4th call returns 429."""
        token = self._generate_jwt(self.user_id, self.user_email)
        for i in range(3):
            resp = self.client.post(
                f"/tickets/{self.txn.transaction_id}/email/",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                format="json",
            )
            self.assertEqual(resp.status_code, 200, f"Attempt {i+1} should succeed")

        resp_4 = self.client.post(
            f"/tickets/{self.txn.transaction_id}/email/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            format="json",
        )
        self.assertEqual(resp_4.status_code, 429)
        self.assertEqual(resp_4.data.get("code"), "RATE_LIMITED")

    @patch("resend.Emails.send")
    def test_email_task_execution_sends_pdf_attachment(self, mock_resend_send):
        """Celery task executes, generates PDF attachment and sends to owner email via resend."""
        with self.settings(RESEND_API_KEY="re_test_key_123"):
            mock_resend_send.return_value = {"id": "email_123"}
            res = send_ticket_email_task(str(self.txn.transaction_id))

            self.assertEqual(res, "sent")
            mock_resend_send.assert_called_once()
            call_args = mock_resend_send.call_args[0][0]
            self.assertEqual(call_args["to"], [self.user_email])
            self.assertIn("Coldplay", call_args["subject"])

            attachments = call_args["attachments"]
            self.assertEqual(len(attachments), 1)
            att = attachments[0]
            self.assertTrue(att["filename"].endswith(".pdf"))
            # Content should be list of ints / bytes
            content_bytes = bytes(att["content"])
            self.assertTrue(content_bytes.startswith(b"%PDF"))

    @patch("resend.Emails.send")
    def test_email_task_retries_on_failure(self, mock_resend_send):
        """Task retries on exception raised by email provider."""
        with self.settings(RESEND_API_KEY="re_test_key_123"):
            mock_resend_send.side_effect = Exception("SMTP Connection Failed")
            with patch.object(send_ticket_email_task, "retry", side_effect=Exception("Task Retry Triggered")) as mock_retry:
                with self.assertRaises(Exception) as ctx:
                    send_ticket_email_task(str(self.txn.transaction_id))
                self.assertIn("Task Retry Triggered", str(ctx.exception))
                mock_retry.assert_called_once()
