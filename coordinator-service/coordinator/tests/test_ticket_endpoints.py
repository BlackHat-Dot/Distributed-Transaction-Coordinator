import concurrent.futures
import time
import uuid
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.conf import settings
from django.test import TransactionTestCase
from django.utils import timezone
import jwt
from rest_framework.test import APIClient

from coordinator.models import Transaction, Ticket
from coordinator.ticket_generator import generate_qr_code_image


class TicketEndpointTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        self.user_id = "usr_alice"
        self.user_email = "alice@example.com"
        self.secret_key = settings.SECRET_KEY
        self.gate_key = getattr(settings, "GATE_API_KEY", "gate-secret-key-change-in-production")

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

        from django.core.cache import cache
        cache.clear()

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


    def _generate_jwt(self, user_id, email="test@example.com"):
        payload = {
            "user_id": user_id,
            "email": email,
            "exp": time.time() + 3600,
            "iat": time.time(),
        }
        return jwt.encode(payload, self.secret_key, algorithm="HS256")

    def test_auth_real_signed_jwt_success(self):
        """Owner can access ticket details using a real signed JWT in Authorization header."""
        token = self._generate_jwt(self.user_id, self.user_email)
        resp = self.client.get(
            f"/tickets/{self.txn.transaction_id}/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("tickets", resp.data)
        self.assertIn("event", resp.data)
        self.assertEqual(len(resp.data["tickets"]), 1)
        self.assertEqual(resp.data["tickets"][0]["ticket_code"], self.ticket.ticket_code)
        self.assertTrue(resp.data["tickets"][0]["qr_code_base64"].startswith("data:image/png;base64,"))

    def test_auth_unauthenticated_returns_401(self):
        """Unauthenticated requests must return 401 Unauthorized."""
        resp = self.client.get(f"/tickets/{self.txn.transaction_id}/")
        self.assertEqual(resp.status_code, 401)

        resp_pdf = self.client.get(f"/tickets/{self.txn.transaction_id}/pdf/")
        self.assertEqual(resp_pdf.status_code, 401)

        resp_ics = self.client.get(f"/tickets/{self.txn.transaction_id}/calendar.ics")
        self.assertEqual(resp_ics.status_code, 401)

    def test_auth_token_in_query_param_rejected_with_401(self):
        """Ticket endpoints must reject ?token= in query string and return 401."""
        token = self._generate_jwt(self.user_id, self.user_email)
        resp = self.client.get(f"/tickets/{self.txn.transaction_id}/?token={token}")
        self.assertEqual(resp.status_code, 401)

        resp_pdf = self.client.get(f"/tickets/{self.txn.transaction_id}/pdf/?token={token}")
        self.assertEqual(resp_pdf.status_code, 401)

        resp_ics = self.client.get(f"/tickets/{self.txn.transaction_id}/calendar.ics?token={token}")
        self.assertEqual(resp_ics.status_code, 401)

    def test_auth_non_owner_returns_404_not_403(self):
        """Non-owner accessing another user's transaction must receive 404 (not 403)."""
        bob_token = self._generate_jwt("usr_bob", "bob@example.com")
        
        resp = self.client.get(
            f"/tickets/{self.txn.transaction_id}/",
            HTTP_AUTHORIZATION=f"Bearer {bob_token}",
        )
        self.assertEqual(resp.status_code, 404)

        resp_pdf = self.client.get(
            f"/tickets/{self.txn.transaction_id}/pdf/",
            HTTP_AUTHORIZATION=f"Bearer {bob_token}",
        )
        self.assertEqual(resp_pdf.status_code, 404)

        resp_ics = self.client.get(
            f"/tickets/{self.txn.transaction_id}/calendar.ics",
            HTTP_AUTHORIZATION=f"Bearer {bob_token}",
        )
        self.assertEqual(resp_ics.status_code, 404)

    def test_get_ticket_pdf_endpoint(self):
        """GET /tickets/{id}/pdf/ returns PDF file attachment with proper content type and header."""
        token = self._generate_jwt(self.user_id, self.user_email)
        resp = self.client.get(
            f"/tickets/{self.txn.transaction_id}/pdf/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn(".pdf", resp["Content-Disposition"])
        self.assertTrue(resp.content.startswith(b"%PDF"))

    def test_get_ticket_ics_endpoint(self):
        """GET /tickets/{id}/calendar.ics returns valid iCalendar file."""
        token = self._generate_jwt(self.user_id, self.user_email)
        resp = self.client.get(
            f"/tickets/{self.txn.transaction_id}/calendar.ics",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/calendar; charset=utf-8")
        self.assertIn("BEGIN:VCALENDAR", resp.content.decode("utf-8"))

    def test_public_verify_page_no_pii(self):
        """Public /verify/{ticket_code}/ page returns status VALID and contains NO PII (no email/uuid/user_id)."""
        resp = self.client.get(f"/verify/{self.ticket.ticket_code}/")
        self.assertEqual(resp.status_code, 200)
        content_text = resp.content.decode("utf-8")

        self.assertIn("VALID", content_text)
        self.assertIn("Coldplay", content_text)
        self.assertIn("A1", content_text)

        # Assert no sensitive PII
        self.assertNotIn(self.user_email, content_text)
        self.assertNotIn(self.user_id, content_text)
        self.assertNotIn(str(self.txn.transaction_id), content_text)

    def test_public_verify_invalid_code(self):
        """Public /verify/{ticket_code}/ for nonexistent code returns 404 / INVALID."""
        resp = self.client.get("/verify/invalid_nonexistent_code/")
        self.assertEqual(resp.status_code, 404)
        content_text = resp.content.decode("utf-8")
        self.assertIn("INVALID", content_text)

    def test_gate_redeem_requires_gate_api_key(self):
        """POST /gate/redeem/ requires valid GATE_API_KEY; missing or invalid returns 401."""
        # Missing header
        resp = self.client.post("/gate/redeem/", {"ticket_code": self.ticket.ticket_code}, format="json")
        self.assertEqual(resp.status_code, 401)

        # Wrong key
        resp = self.client.post(
            "/gate/redeem/",
            {"ticket_code": self.ticket.ticket_code},
            HTTP_X_GATE_API_KEY="wrong-secret-key",
            format="json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_gate_redeem_atomic_and_idempotent_409(self):
        """First gate redemption succeeds (200), subsequent calls return 409 ALREADY_USED."""
        # First call succeeds
        resp1 = self.client.post(
            "/gate/redeem/",
            {"ticket_code": self.ticket.ticket_code},
            HTTP_X_GATE_API_KEY=self.gate_key,
            format="json",
        )
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.data.get("status"), "REDEEMED")
        used_at_str = resp1.data.get("used_at")
        self.assertIsNotNone(used_at_str)

        # Ticket in DB must have used_at set
        self.ticket.refresh_from_db()
        self.assertIsNotNone(self.ticket.used_at)

        # Verify endpoint now shows ALREADY USED
        verify_resp = self.client.get(f"/verify/{self.ticket.ticket_code}/")
        self.assertEqual(verify_resp.status_code, 200)
        self.assertIn("ALREADY USED", verify_resp.content.decode("utf-8"))

        # Second call returns 409
        resp2 = self.client.post(
            "/gate/redeem/",
            {"ticket_code": self.ticket.ticket_code},
            HTTP_X_GATE_API_KEY=self.gate_key,
            format="json",
        )
        self.assertEqual(resp2.status_code, 409)
        self.assertEqual(resp2.data.get("code"), "ALREADY_USED")
        self.assertEqual(resp2.data.get("used_at"), used_at_str)

    def test_gate_redeem_multi_seat_independent(self):
        """In a 3-seat booking, redeeming seat 1 leaves seats 2 and 3 still VALID."""
        import secrets
        t2 = Ticket.objects.create(
            transaction=self.txn,
            seat_id=2,
            seat_label="A2",
            event_name="Coldplay",
            ticket_code=secrets.token_urlsafe(16),
        )
        t3 = Ticket.objects.create(
            transaction=self.txn,
            seat_id=3,
            seat_label="A3",
            event_name="Coldplay",
            ticket_code=secrets.token_urlsafe(16),
        )

        # Redeem seat 1
        resp = self.client.post(
            "/gate/redeem/",
            {"ticket_code": self.ticket.ticket_code},
            HTTP_X_GATE_API_KEY=self.gate_key,
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

        # Seat 1 is ALREADY USED
        v1 = self.client.get(f"/verify/{self.ticket.ticket_code}/")
        self.assertIn("ALREADY USED", v1.content.decode("utf-8"))

        # Seat 2 is VALID
        v2 = self.client.get(f"/verify/{t2.ticket_code}/")
        self.assertIn("VALID", v2.content.decode("utf-8"))
        self.assertNotIn("ALREADY USED", v2.content.decode("utf-8"))

        # Seat 3 is VALID
        v3 = self.client.get(f"/verify/{t3.ticket_code}/")
        self.assertIn("VALID", v3.content.decode("utf-8"))
        self.assertNotIn("ALREADY USED", v3.content.decode("utf-8"))

    def test_gate_redeem_refuses_if_transaction_not_completed(self):
        """Redeem refuses with 409 if transaction status is no longer COMPLETED (e.g. COMPENSATED/refunded)."""
        self.txn.status = Transaction.Status.COMPENSATED
        self.txn.save()

        resp = self.client.post(
            "/gate/redeem/",
            {"ticket_code": self.ticket.ticket_code},
            HTTP_X_GATE_API_KEY=self.gate_key,
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "TRANSACTION_NOT_COMPLETED")

    def test_gate_redeem_concurrency_10_requests(self):
        """10 concurrent POST /gate/redeem/ for one code -> exactly 1 success (200) and 9 x 409."""
        ticket_code = self.ticket.ticket_code
        gate_key = self.gate_key

        def _worker(worker_id):
            try:
                c = APIClient()
                resp = c.post(
                    "/gate/redeem/",
                    {"ticket_code": ticket_code},
                    HTTP_X_GATE_API_KEY=gate_key,
                    REMOTE_ADDR=f"10.200.1.{worker_id}",
                    format="json",
                )
                return resp.status_code
            finally:
                from django.db import connections
                connections.close_all()

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_worker, i) for i in range(10)]
            statuses = [f.result() for f in futures]


        self.assertEqual(statuses.count(200), 1, f"Expected exactly 1 HTTP 200, got: {statuses}")
        self.assertEqual(statuses.count(409), 9, f"Expected exactly 9 HTTP 409, got: {statuses}")

        self.ticket.refresh_from_db()
        self.assertIsNotNone(self.ticket.used_at)

    def test_verify_ticket_revoked_if_transaction_compensated(self):
        """When transaction is no longer COMPLETED (e.g. COMPENSATED), public /verify/ returns REVOKED with no seat/event details."""
        self.txn.status = Transaction.Status.COMPENSATED
        self.txn.save()

        # JSON response
        resp = self.client.get(
            f"/verify/{self.ticket.ticket_code}/",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), "REVOKED")
        self.assertNotIn("event_name", resp.data)
        self.assertNotIn("seat_label", resp.data)
        self.assertNotIn("venue", resp.data)

        # HTML response
        html_resp = self.client.get(f"/verify/{self.ticket.ticket_code}/")
        html_text = html_resp.content.decode("utf-8")
        self.assertIn("REVOKED", html_text)
        self.assertNotIn("Coldplay", html_text)
        self.assertNotIn("Grand Arena Stadium", html_text)

    def test_auth_debug_true_and_is_testing_false_still_rejects_unauth_and_non_owner(self):
        """With DEBUG=True and IS_TESTING=False, unauthenticated requests still get 401 and non-owners get 404."""
        from django.test import override_settings

        with override_settings(DEBUG=True):
            with patch("coordinator.ticket_views.settings.DEBUG", True), \
                 patch.object(settings, "IS_TESTING", False):

                # 1. Unauthenticated -> 401
                unauth_resp = self.client.get(f"/tickets/{self.txn.transaction_id}/")
                self.assertEqual(unauth_resp.status_code, 401)

                unauth_pdf = self.client.get(f"/tickets/{self.txn.transaction_id}/pdf/")
                self.assertEqual(unauth_pdf.status_code, 401)

                # 2. Non-owner with valid signed JWT -> 404 (not 403)
                eve_token = self._generate_jwt("usr_eve", "eve@example.com")
                non_owner_resp = self.client.get(
                    f"/tickets/{self.txn.transaction_id}/",
                    HTTP_AUTHORIZATION=f"Bearer {eve_token}",
                )
                self.assertEqual(non_owner_resp.status_code, 404)

    def test_gate_redeem_rate_limit_11th_attempt_returns_429(self):
        """11th bad-key attempt in a minute to /gate/redeem/ returns 429."""
        from django.core.cache import cache
        cache.clear()

        # Send 10 invalid requests
        for i in range(10):
            r = self.client.post(
                "/gate/redeem/",
                {"ticket_code": self.ticket.ticket_code},
                HTTP_X_GATE_API_KEY="bad_key_attempt",
                REMOTE_ADDR="192.168.1.100",
                format="json",
            )
            self.assertEqual(r.status_code, 401, f"Attempt {i+1} should be 401")

        # 11th request must return 429 Too Many Requests
        r11 = self.client.post(
            "/gate/redeem/",
            {"ticket_code": self.ticket.ticket_code},
            HTTP_X_GATE_API_KEY="bad_key_attempt",
            REMOTE_ADDR="192.168.1.100",
            format="json",
        )
        self.assertEqual(r11.status_code, 429)
        self.assertEqual(r11.data.get("code"), "RATE_LIMITED")

    def test_verify_rate_limit_returns_429(self):
        """Public /verify/ endpoint enforces rate limiting and returns 429."""
        from django.core.cache import cache
        cache.clear()

        # The limit is 60/min. Make 60 requests.
        for _ in range(60):
            r = self.client.get(
                f"/verify/{self.ticket.ticket_code}/",
                REMOTE_ADDR="10.0.0.1",
            )
            self.assertEqual(r.status_code, 200)

        # 61st request returns 429
        r61 = self.client.get(
            f"/verify/{self.ticket.ticket_code}/",
            REMOTE_ADDR="10.0.0.1",
        )
        self.assertEqual(r61.status_code, 429)

    def test_rotating_x_forwarded_for_from_untrusted_peer_does_not_bypass_gate_rate_limit(self):
        """Rotating spoofed X-Forwarded-For headers from untrusted peer cannot bypass gate rate limiting."""
        from django.core.cache import cache
        cache.clear()

        # Send 10 invalid requests with changing X-Forwarded-For from an untrusted peer
        for i in range(10):
            r = self.client.post(
                "/gate/redeem/",
                {"ticket_code": self.ticket.ticket_code},
                HTTP_X_GATE_API_KEY="bad_key_attempt",
                HTTP_X_FORWARDED_FOR=f"203.0.113.{i+1}",
                REMOTE_ADDR="198.51.100.25",  # Untrusted peer
                format="json",
            )
            self.assertEqual(r.status_code, 401)

        # 11th attempt from same peer (even with new spoofed IP) must be blocked with 429
        r11 = self.client.post(
            "/gate/redeem/",
            {"ticket_code": self.ticket.ticket_code},
            HTTP_X_GATE_API_KEY="bad_key_attempt",
            HTTP_X_FORWARDED_FOR="198.18.0.99",
            REMOTE_ADDR="198.51.100.25",
            format="json",
        )
        self.assertEqual(r11.status_code, 429)
        self.assertEqual(r11.data.get("code"), "RATE_LIMITED")

    def test_rotating_x_forwarded_for_from_untrusted_peer_does_not_bypass_verify_rate_limit(self):
        """Rotating spoofed X-Forwarded-For headers from untrusted peer cannot bypass verify rate limiting."""
        from django.core.cache import cache
        cache.clear()

        # The limit is 60/min. Make 60 requests with rotating XFF from untrusted peer.
        for i in range(60):
            r = self.client.get(
                f"/verify/{self.ticket.ticket_code}/",
                HTTP_X_FORWARDED_FOR=f"10.{i // 256}.{i % 256}.1",
                REMOTE_ADDR="198.51.100.30",  # Untrusted peer
            )
            self.assertEqual(r.status_code, 200)

        # 61st request must be rate-limited (429)
        r61 = self.client.get(
            f"/verify/{self.ticket.ticket_code}/",
            HTTP_X_FORWARDED_FOR="10.255.255.1",
            REMOTE_ADDR="198.51.100.30",
        )
        self.assertEqual(r61.status_code, 429)

    def test_verify_page_valid_before_redeem_and_already_used_after(self):
        """GET /verify/ shows VALID before redeem and ALREADY USED after redeem."""
        from django.core.cache import cache
        cache.clear()

        # Before redeem
        v_before = self.client.get(f"/verify/{self.ticket.ticket_code}/")
        self.assertEqual(v_before.status_code, 200)
        self.assertIn("VALID", v_before.content.decode("utf-8"))
        self.assertNotIn("ALREADY USED", v_before.content.decode("utf-8"))

        # Redeem via gate
        r_redeem = self.client.post(
            "/gate/redeem/",
            {"ticket_code": self.ticket.ticket_code},
            HTTP_X_GATE_API_KEY=self.gate_key,
            format="json",
        )
        self.assertEqual(r_redeem.status_code, 200)

        # After redeem
        v_after = self.client.get(f"/verify/{self.ticket.ticket_code}/")
        self.assertEqual(v_after.status_code, 200)
        self.assertIn("ALREADY USED", v_after.content.decode("utf-8"))

    def test_cors_preflight_gate_api_key(self):
        """OPTIONS preflight requesting x-gate-api-key returns it in Access-Control-Allow-Headers."""
        response = self.client.options(
            "/gate/redeem/",
            HTTP_ORIGIN="http://localhost:3000",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="x-gate-api-key",
        )
        self.assertEqual(response.status_code, 200)
        allow_headers = response.headers.get("Access-Control-Allow-Headers", "").lower()
        self.assertIn("x-gate-api-key", [h.strip() for h in allow_headers.split(",")])


