from datetime import timedelta
import json
import time
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from coordinator.auth_views import (
    RequesterIdentity,
    _create_jwt,
    _get_caller,
    _get_client_ip,
    _get_redis_client,
    _is_admin_email,
    _normalize_email,
    validate_email,
    verify_jwt,
)


class AuthAndSessionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.email = "shopper@gmail.com"
        cache.clear()
        try:
            r = _get_redis_client()
            if r:
                r.flushdb()
        except Exception:
            pass

    def tearDown(self):
        cache.clear()
        try:
            r = _get_redis_client()
            if r:
                r.flushdb()
        except Exception:
            pass

    def _set_cache_and_redis(self, key, value, timeout=30):
        cache.set(key, value, timeout=timeout)
        try:
            r = _get_redis_client()
            if r:
                r.setex(key, timeout, value)
        except Exception:
            pass

    def test_validate_email_domain_and_typo_suggestions(self):
        """Email validation requires valid format, allows gmail, and provides typo suggestions."""
        valid, _ = validate_email("user.test@gmail.com")
        self.assertTrue(valid)

        # Invalid format: consecutive dots
        valid, err = validate_email("user..name@gmail.com")
        self.assertFalse(valid)
        self.assertIn("consecutive dots", err)

        # Known typo suggestions
        valid, err = validate_email("user@gmial.com")
        self.assertFalse(valid)
        self.assertIn("Did you mean user@gmail.com?", err)

        # Non-gmail external domain should NOT suggest gmail
        valid, err = validate_email("user@yahoo.com")
        self.assertFalse(valid)
        self.assertNotIn("Did you mean", err)
        self.assertIn("Only @gmail.com", err)

    def test_send_otp_success_creates_cached_otp(self):
        """Sending OTP for valid email generates 6-digit code in cache and responds 200 OK."""
        with patch("coordinator.auth_views._is_dev_auth_mode", return_value=False), \
             patch("coordinator.auth_views._dispatch_otp_email") as mock_dispatch:
            resp = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.data.get("success"))
            mock_dispatch.assert_not_called()  # In testing mode (IS_TESTING=True), email thread is suppressed

        cached_otp = cache.get(f"dtc:otp:{self.email}")
        self.assertIsNotNone(cached_otp)
        self.assertEqual(len(str(cached_otp)), 6)
        self.assertTrue(str(cached_otp).isdigit())

    def test_send_otp_blocked_if_active_session_exists(self):
        """Single-device guard: send_otp returns 409 CONCURRENT_LOGIN_BLOCKED if an active session exists."""
        self._set_cache_and_redis(f"dtc:session:{self.email}", "active_session_123", timeout=30)

        resp = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "CONCURRENT_LOGIN_BLOCKED")

    def test_verify_otp_success_issues_jwt_and_clears_otp(self):
        """Correct OTP issues signed JWT with session ID, establishes active session, and deletes OTP."""
        otp_code = "654321"
        self._set_cache_and_redis(f"dtc:otp:{self.email}", otp_code, timeout=300)

        resp = self.client.post(
            "/auth/verify-otp/",
            {"email": self.email, "otp": otp_code},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.get("success"))
        token = resp.data.get("token")
        self.assertIsNotNone(token)

        # Verify decoded payload has session id and email
        payload, err = verify_jwt(token)
        self.assertIsNone(err)
        self.assertEqual(payload.get("user_email"), self.email)
        self.assertIn("sid", payload)

        # Single-use: OTP must be wiped from cache
        self.assertIsNone(cache.get(f"dtc:otp:{self.email}"))

        # Active session must be registered in cache
        active_sid = cache.get(f"dtc:session:{self.email}")
        self.assertEqual(active_sid, payload["sid"])

    def test_verify_otp_failure_and_brute_force_lockout(self):
        """Failed OTP submissions decrement remaining attempts; 5 failures trigger 24h lockout."""
        self._set_cache_and_redis(f"dtc:otp:{self.email}", "123456", timeout=300)

        with patch("coordinator.auth_views._is_dev_auth_mode", return_value=False):
            # Attempts 1 to 4 should return 400 INVALID_OTP
            for i in range(1, 5):
                resp = self.client.post(
                    "/auth/verify-otp/",
                    {"email": self.email, "otp": "000000"},
                    format="json",
                )
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.data.get("code"), "INVALID_OTP")
                self.assertEqual(resp.data.get("attempts_remaining"), 5 - i)

            # 5th attempt must trigger 429 lockout
            resp = self.client.post(
                "/auth/verify-otp/",
                {"email": self.email, "otp": "000000"},
                format="json",
            )
            self.assertEqual(resp.status_code, 429)
            self.assertEqual(resp.data.get("code"), "IP_EMAIL_BLOCKED")

            # Subsequent attempt blocked before evaluating OTP
            resp = self.client.post(
                "/auth/verify-otp/",
                {"email": self.email, "otp": "123456"},
                format="json",
            )
            self.assertEqual(resp.status_code, 429)
            self.assertEqual(resp.data.get("code"), "IP_EMAIL_BLOCKED")

    def test_verify_otp_blocked_when_active_session_already_present(self):
        """Concurrent verify_otp attempts reject with 409 CONCURRENT_LOGIN_BLOCKED."""
        self._set_cache_and_redis(f"dtc:otp:{self.email}", "123456", timeout=300)
        self._set_cache_and_redis(f"dtc:session:{self.email}", "existing_device_sid", timeout=30)

        resp = self.client.post(
            "/auth/verify-otp/",
            {"email": self.email, "otp": "123456"},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "CONCURRENT_LOGIN_BLOCKED")

    def test_get_me_returns_profile_and_staff_role(self):
        """get_me returns user profile, distinguishing regular users from admin domain staff."""
        admin_email = "boss@dtc.internal"
        with patch.dict("os.environ", {"ADMIN_DOMAIN": "dtc.internal"}):
            user_jwt = _create_jwt("usr_123", self.email, session_id="sid_user")
            self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {user_jwt}")
            resp = self.client.get("/auth/me/")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data["user"]["user_email"], self.email)
            self.assertFalse(resp.data["user"]["is_staff"])

            admin_jwt = _create_jwt("usr_admin", admin_email, session_id="sid_admin")
            self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {admin_jwt}")
            resp = self.client.get("/auth/me/")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data["user"]["user_email"], admin_email)
            self.assertTrue(resp.data["user"]["is_staff"])

    def test_session_superseded_detection_in_get_me_and_heartbeat(self):
        """When an older session is superseded by a new login, heartbeat and get_me return 401 SESSION_SUPERSEDED."""
        old_sid = "sid_device_a"
        new_sid = "sid_device_b"
        old_jwt = _create_jwt("usr_123", self.email, session_id=old_sid)

        # Device B logs in, setting the active session key to new_sid
        self._set_cache_and_redis(f"dtc:session:{self.email}", new_sid, timeout=30)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {old_jwt}")

        # get_me should detect supersession
        resp = self.client.get("/auth/me/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data.get("code"), "SESSION_SUPERSEDED")

        # heartbeat should also detect supersession
        resp = self.client.post("/auth/heartbeat/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data.get("code"), "SESSION_SUPERSEDED")

    def test_heartbeat_extends_session_ttl_for_active_session(self):
        """Active session heartbeat refreshes session TTL and returns status alive."""
        sid = "sid_active"
        jwt_token = _create_jwt("usr_123", self.email, session_id=sid)
        self._set_cache_and_redis(f"dtc:session:{self.email}", sid, timeout=30)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {jwt_token}")
        resp = self.client.post("/auth/heartbeat/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), "alive")

        # Session should still be active with matching sid
        self.assertEqual(cache.get(f"dtc:session:{self.email}"), sid)

    def test_logout_invalidates_session_idempotently(self):
        """Logout purges session keys and returns 200 OK even when called repeatedly."""
        sid = "sid_to_logout"
        jwt_token = _create_jwt("usr_123", self.email, session_id=sid)
        self._set_cache_and_redis(f"dtc:session:{self.email}", sid, timeout=30)
        self._set_cache_and_redis("dtc:session:usr_123", sid, timeout=30)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {jwt_token}")
        resp = self.client.post("/auth/logout/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), "logged_out")

        # Session keys must be cleared
        self.assertIsNone(cache.get(f"dtc:session:{self.email}"))
        self.assertIsNone(cache.get("dtc:session:usr_123"))

        # Idempotent re-invocation succeeds
        resp2 = self.client.post("/auth/logout/")
        self.assertEqual(resp2.status_code, 200)

    def test_caller_identity_resolution_staff_key(self):
        """_get_caller correctly validates X-Staff-Key with constant-time check or fails."""
        from django.test import RequestFactory
        from rest_framework.exceptions import AuthenticationFailed

        rf = RequestFactory()
        with patch.dict("os.environ", {"STAFF_API_KEY": "secret-staff-key"}):
            # Valid staff key
            request = rf.get("/", HTTP_X_STAFF_KEY="secret-staff-key")
            identity = _get_caller(request)
            self.assertTrue(identity.is_authenticated)
            self.assertTrue(identity.is_staff)

            # Invalid staff key
            request_bad = rf.get("/", HTTP_X_STAFF_KEY="wrong-key")
            with self.assertRaises(AuthenticationFailed):
                _get_caller(request_bad)
