from datetime import timedelta
import json
import time
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, SimpleTestCase, RequestFactory
from rest_framework.exceptions import Throttled
from rest_framework.test import APIClient

from coordinator.throttling import TransactionReadRateThrottle, coordinator_exception_handler

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


class CoordinatorThrottlingTests(SimpleTestCase):
    def setUp(self):
        self.rf = RequestFactory()

    def test_get_throttled_message_informs_client_checking_too_often(self):
        """GET request throttling returns explicit 'checking status too often' message with wait time."""
        request = self.rf.get("/transactions/123/")
        exc = Throttled(wait=15)
        resp = coordinator_exception_handler(exc, {"request": request})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data.get("code"), "THROTTLED")
        self.assertEqual(resp.data.get("wait"), 15)
        self.assertEqual(resp.data.get("available_in_seconds"), 15)
        self.assertIn("You are checking status too often", resp.data.get("error"))
        self.assertIn("15 seconds", resp.data.get("error"))

    def test_post_throttled_message_standard_rate_limit(self):
        """POST request throttling returns standard request limit exceeded message with wait time."""
        request = self.rf.post("/transactions/")
        exc = Throttled(wait=30)
        resp = coordinator_exception_handler(exc, {"request": request})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data.get("code"), "THROTTLED")
        self.assertEqual(resp.data.get("wait"), 30)
        self.assertIn("Request limit exceeded", resp.data.get("error"))

    def test_transaction_read_rate_throttle_rate(self):
        """TransactionReadRateThrottle uses raised read rate (60/min)."""
        throttle = TransactionReadRateThrottle()
        self.assertEqual(throttle.get_rate(), "60/min")

    def test_safe_rate_throttle_handles_value_error_on_expired_key(self):
        """When cache.incr raises ValueError (key expired between add and incr), it recovers safely."""
        from coordinator.throttling import SafeRateThrottle
        class DummyThrottle(SafeRateThrottle):
            rate = "5/min"
        throttle = DummyThrottle()
        request = self.rf.get("/test/")

        with patch.object(throttle, "get_cache_key", return_value="dummy_key"), \
             patch.object(throttle.cache, "incr", side_effect=ValueError("Key not found")), \
             patch.object(throttle.cache, "set") as mock_set:
            allowed = throttle.allow_request(request, None)
            self.assertTrue(allowed)
            mock_set.assert_called_once()

    def test_safe_rate_throttle_does_not_catch_generic_backend_exceptions(self):
        """Generic backend errors (e.g. connection error) are not caught by ValueError, preventing counter reset."""
        from coordinator.throttling import SafeRateThrottle
        class DummyThrottle(SafeRateThrottle):
            rate = "5/min"
        throttle = DummyThrottle()
        request = self.rf.get("/test/")

        with patch.object(throttle, "get_cache_key", return_value="dummy_key"), \
             patch.object(throttle.cache, "incr", side_effect=RuntimeError("Redis connection lost")):
            with self.assertRaises(RuntimeError):
                throttle.allow_request(request, None)

    def test_is_booking_authenticated_checks_throttles_before_raising(self):
        """Unauthenticated caller triggers view.check_throttles before raising NotAuthenticated."""
        from coordinator.throttling import IsBookingAuthenticated
        from rest_framework.exceptions import NotAuthenticated
        from unittest.mock import MagicMock

        perm = IsBookingAuthenticated()
        mock_view = MagicMock()
        request = self.rf.get("/test/")

        with patch("coordinator.throttling._get_caller", return_value=None):
            with self.assertRaises(NotAuthenticated):
                perm.has_permission(request, mock_view)

        mock_view.check_throttles.assert_called_once_with(request)

    def test_wait_returns_exact_ttl_when_available(self):
        """wait() returns exact TTL from Redis cache when available, else duration."""
        from coordinator.throttling import SafeRateThrottle
        from unittest.mock import MagicMock

        class DummyThrottle(SafeRateThrottle):
            rate = "5/min"
        throttle = DummyThrottle()
        throttle.key = "test_throttle_key"

        # 1. When ttl is available (e.g. 14s remaining)
        mock_cache = MagicMock()
        mock_cache.ttl.return_value = 14
        with patch.object(throttle, "cache", mock_cache):
            self.assertEqual(throttle.wait(), 14)

        # 2. When ttl is unavailable, falls back to full duration (60s)
        mock_cache_no_ttl = MagicMock(spec=[])  # no ttl attribute
        with patch.object(throttle, "cache", mock_cache_no_ttl):
            self.assertEqual(throttle.wait(), 60)

    def test_allow_request_touches_key_on_first_count(self):
        """When count reaches 1, cache.touch is called to guarantee a TTL on new window."""
        from coordinator.throttling import SafeRateThrottle
        from unittest.mock import MagicMock

        class DummyThrottle(SafeRateThrottle):
            rate = "5/min"
        throttle = DummyThrottle()
        request = self.rf.get("/test/")

        # 1. When count == 1, touch is called
        mock_cache = MagicMock()
        mock_cache.incr.return_value = 1
        with patch.object(throttle, "get_cache_key", return_value="dummy_key"), \
             patch.object(throttle, "cache", mock_cache):
            allowed = throttle.allow_request(request, None)
            self.assertTrue(allowed)
            mock_cache.touch.assert_called_once_with("dummy_key", throttle.duration)

        # 2. When count > 1, touch is not called again
        mock_cache_subsequent = MagicMock()
        mock_cache_subsequent.incr.return_value = 2
        with patch.object(throttle, "get_cache_key", return_value="dummy_key"), \
             patch.object(throttle, "cache", mock_cache_subsequent):
            allowed = throttle.allow_request(request, None)
            self.assertTrue(allowed)
            mock_cache_subsequent.touch.assert_not_called()






