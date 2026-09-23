import os
import sys
import uuid
from unittest.mock import patch, MagicMock
from django.test import TestCase
from django.core.cache import cache
from rest_framework.test import APIClient
from coordinator.auth_views import _get_redis_client


class AuthOtpLockoutTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.email = "adversary@test.io"
        self.redis_client = _get_redis_client()
        cache.clear()
        if self.redis_client:
            try:
                from coordinator.auth_views import _email_hash
                h = _email_hash(self.email)
                for key in list(self.redis_client.keys(f"dtc:*{self.email}*")) + list(self.redis_client.keys(f"dtc:*{h}*")):
                    self.redis_client.delete(key)
            except Exception:
                pass

    def tearDown(self):
        cache.clear()
        if self.redis_client:
            try:
                from coordinator.auth_views import _email_hash
                h = _email_hash(self.email)
                for key in list(self.redis_client.keys(f"dtc:*{self.email}*")) + list(self.redis_client.keys(f"dtc:*{h}*")):
                    self.redis_client.delete(key)
            except Exception:
                pass

    def test_request_and_verify_otp_success(self):
        # Request OTP
        req_resp = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
        self.assertEqual(req_resp.status_code, 200)
        otp = cache.get(f"dtc:otp:{self.email}")
        if not otp and self.redis_client:
            try:
                otp = self.redis_client.get(f"dtc:otp:{self.email}")
            except Exception:
                pass
        self.assertTrue(bool(otp))

        # Verify OTP
        ver_resp = self.client.post("/auth/verify-otp/", {"email": self.email, "otp": otp}, format="json")
        self.assertEqual(ver_resp.status_code, 200)
        self.assertTrue(ver_resp.data.get("success"))
        self.assertTrue(bool(ver_resp.data.get("token")))

    def test_wrong_otp_returns_400_with_remaining_attempts(self):
        req_resp = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
        self.assertEqual(req_resp.status_code, 200)

        # 1st wrong attempt
        ver_resp = self.client.post("/auth/verify-otp/", {"email": self.email, "otp": "000000"}, format="json")
        self.assertEqual(ver_resp.status_code, 400)
        self.assertEqual(ver_resp.data.get("attempts_remaining"), 4)

    def test_five_wrong_attempts_triggers_15min_lockout(self):
        req_resp = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
        self.assertEqual(req_resp.status_code, 200)

        for attempt in range(1, 5):
            ver_resp = self.client.post("/auth/verify-otp/", {"email": self.email, "otp": f"11111{attempt}"}, format="json")
            self.assertEqual(ver_resp.status_code, 400)
            self.assertEqual(ver_resp.data.get("attempts_remaining"), 5 - attempt)

        # 5th wrong attempt must trigger 429 and lockout
        ver_resp5 = self.client.post("/auth/verify-otp/", {"email": self.email, "otp": "999999"}, format="json")
        self.assertEqual(ver_resp5.status_code, 429)
        self.assertIn(ver_resp5.data.get("code"), ["ACCOUNT_LOCKED", "IP_EMAIL_BLOCKED"])

        # Further verification attempts while locked return 429
        ver_resp_locked = self.client.post("/auth/verify-otp/", {"email": self.email, "otp": "123456"}, format="json")
        self.assertEqual(ver_resp_locked.status_code, 429)
        self.assertIn(ver_resp_locked.data.get("code"), ["ACCOUNT_LOCKED", "IP_EMAIL_BLOCKED"])

        # Requesting new OTP while locked must also be rejected with 429
        req_locked = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
        self.assertEqual(req_locked.status_code, 429)
        self.assertIn(req_locked.data.get("code"), ["ACCOUNT_LOCKED", "IP_EMAIL_BLOCKED"])

    @patch("coordinator.auth_views._is_dev_auth_mode", return_value=False)
    def test_daily_key_expiry_set_on_first_request(self, mock_is_dev):
        resp = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
        self.assertEqual(resp.status_code, 200)
        if self.redis_client:
            daily_key = f"dtc:otp_daily:{self.email}"
            ttl = self.redis_client.ttl(daily_key)
            self.assertGreater(ttl, 0, f"Expected positive TTL for {daily_key}, got {ttl}")
            self.assertLessEqual(ttl, 86400)


class LocalDevAuthNumbModeTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.email = "devuser@local.io"
        self.redis_client = _get_redis_client()
        cache.clear()
        if self.redis_client:
            try:
                for key in self.redis_client.keys(f"dtc:*{self.email}*"):
                    self.redis_client.delete(key)
            except Exception:
                pass

    def tearDown(self):
        cache.clear()
        if self.redis_client:
            try:
                for key in self.redis_client.keys(f"dtc:*{self.email}*"):
                    self.redis_client.delete(key)
            except Exception:
                pass

    from unittest.mock import patch

    @patch("coordinator.auth_views._is_dev_auth_mode", return_value=True)
    def test_local_dev_send_otp_infinite_resend_bypasses_cooldown_and_daily_limits(self, mock_is_dev):
        for i in range(12):
            resp = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data.get("cooldown_remaining"), 0)
            self.assertEqual(resp.data.get("chances_remaining"), "∞")
            self.assertTrue(resp.data.get("is_local"))
            self.assertTrue(bool(resp.data.get("otp")))

        # Redis rate-limiting keys must be completely absent/numb
        if self.redis_client:
            try:
                self.assertFalse(self.redis_client.exists(f"dtc:otp_cooldown:{self.email}"))
                self.assertFalse(self.redis_client.exists(f"dtc:otp_daily_blocked:{self.email}"))
                self.assertFalse(self.redis_client.exists(f"dtc:otp_session_blocked:{self.email}"))
            except Exception:
                pass

    @patch("coordinator.auth_views._is_dev_auth_mode", return_value=True)
    def test_local_dev_verify_otp_infinite_wrong_attempts_does_not_lockout(self, mock_is_dev):
        send_resp = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
        self.assertEqual(send_resp.status_code, 200)
        otp = send_resp.data.get("otp")

        # Attempt 10 wrong guesses
        for attempt in range(10):
            ver_resp = self.client.post("/auth/verify-otp/", {"email": self.email, "otp": f"00000{attempt}"}, format="json")
            self.assertEqual(ver_resp.status_code, 400)
            self.assertEqual(ver_resp.data.get("attempts_remaining"), "∞")
            self.assertEqual(ver_resp.data.get("code"), "INVALID_OTP")

        # Redis fail count & lock keys must NOT exist
        if self.redis_client:
            try:
                self.assertFalse(self.redis_client.exists(f"dtc:otp_fail_count:{self.email}"))
                self.assertFalse(self.redis_client.exists(f"dtc:otp_locked:{self.email}"))
            except Exception:
                pass

        # Valid OTP still succeeds after all wrong attempts
        success_resp = self.client.post("/auth/verify-otp/", {"email": self.email, "otp": otp}, format="json")
        self.assertEqual(success_resp.status_code, 200)
        self.assertTrue(success_resp.data.get("success"))
        self.assertTrue(bool(success_resp.data.get("token")))

    @patch("coordinator.auth_views._is_dev_auth_mode", return_value=True)
    def test_local_dev_jwt_token_expires_in_30_minutes(self, mock_is_dev):
        from coordinator.auth_views import verify_jwt
        send_resp = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
        otp = send_resp.data.get("otp")

        ver_resp = self.client.post("/auth/verify-otp/", {"email": self.email, "otp": otp}, format="json")
        self.assertEqual(ver_resp.status_code, 200)
        self.assertEqual(ver_resp.data.get("expires_in"), 1800)
        token = ver_resp.data.get("token")

        payload, err = verify_jwt(token)
        self.assertIsNone(err)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["exp"] - payload["iat"], 1800)

    @patch("coordinator.views.settings.DEBUG", False)
    @patch("coordinator.views.settings.IS_TESTING", False)
    def test_production_rejects_unauthenticated_transaction(self):
        import sys
        with patch.object(sys, "argv", ["gunicorn"]):
            resp = self.client.post("/transactions/", {"seat_ids": [1]}, format="json")
            self.assertEqual(resp.status_code, 401)
            self.assertEqual(resp.data.get("code"), "AUTHENTICATION_REQUIRED")

    @patch("coordinator.views.settings.DEBUG", False)
    @patch("coordinator.views.settings.IS_TESTING", False)
    def test_production_accepts_authenticated_jwt_transaction(self):
        import sys
        with patch.object(sys, "argv", ["gunicorn"]):
            from coordinator.auth_views import _create_jwt
            token = _create_jwt("usr_real_fan", "fan@test.io")
            mock_pricing = MagicMock()
            mock_pricing.status_code = 200
            mock_pricing.json.return_value = [{"id": 43, "price": 500, "row": "D", "seat_number": "43"}]
            with patch("coordinator.views.requests.post", return_value=mock_pricing), \
                 patch("coordinator.tasks.execute_saga_task.delay"):
                resp = self.client.post(
                    "/transactions/",
                    {"seat_ids": [43]},
                    HTTP_AUTHORIZATION=f"Bearer {token}",
                    format="json"
                )
                self.assertIn(resp.status_code, [200, 202])
                self.assertEqual(resp.data.get("user_id"), "usr_real_fan")
                self.assertEqual(resp.data.get("user_email"), "fan@test.io")
                self.assertIsNotNone(resp.data.get("transaction_id"))

    @patch("coordinator.views.settings.DEBUG", False)
    @patch("coordinator.views.settings.IS_TESTING", False)
    @patch.dict(os.environ, {"STAFF_API_KEY": "super-secret-staff-key", "STAFF_USER_ID": "usr_staff_01", "STAFF_EMAIL_ID": "staff@dtc.internal"})
    def test_staff_key_allows_booking_without_jwt(self):
        import sys
        with patch.object(sys, "argv", ["gunicorn"]):
            mock_pricing = MagicMock()
            mock_pricing.status_code = 200
            mock_pricing.json.return_value = [{"id": 43, "price": 500, "row": "D", "seat_number": "43"}]
            with patch("coordinator.views.requests.post", return_value=mock_pricing), \
                 patch("coordinator.tasks.execute_saga_task.delay"), \
                 patch("coordinator.views.get_user_from_request") as mock_get_user:
                resp = self.client.post(
                    "/transactions/",
                    {"seat_ids": [43]},
                    HTTP_X_STAFF_KEY="super-secret-staff-key",
                    format="json"
                )
                self.assertIn(resp.status_code, [200, 202])
                self.assertEqual(resp.data.get("user_id"), "usr_staff_01")
                self.assertEqual(resp.data.get("user_email"), "staff@dtc.internal")
                # get_user_from_request must NOT be called when staff key is provided
                mock_get_user.assert_not_called()

    @patch("coordinator.views.settings.DEBUG", False)
    @patch("coordinator.views.settings.IS_TESTING", False)
    @patch.dict(os.environ, {"STAFF_API_KEY": "super-secret-staff-key"})
    def test_staff_key_bypasses_invalid_jwt_token(self):
        import sys
        with patch.object(sys, "argv", ["gunicorn"]):
            mock_pricing = MagicMock()
            mock_pricing.status_code = 200
            mock_pricing.json.return_value = [{"id": 43, "price": 500, "row": "D", "seat_number": "43"}]
            with patch("coordinator.views.requests.post", return_value=mock_pricing), \
                 patch("coordinator.tasks.execute_saga_task.delay"), \
                 patch("coordinator.views.get_user_from_request") as mock_get_user:
                # Even with completely invalid Bearer token, staff key authorizes immediately
                resp = self.client.post(
                    "/transactions/",
                    {"seat_ids": [43]},
                    HTTP_X_STAFF_KEY="super-secret-staff-key",
                    HTTP_AUTHORIZATION="Bearer completely.garbage.token",
                    HTTP_X_USER_ID="custom_staff_id",
                    HTTP_X_USER_EMAIL="custom_staff@dtc.internal",
                    format="json"
                )
                self.assertIn(resp.status_code, [200, 202])
                self.assertEqual(resp.data.get("user_id"), "custom_staff_id")
                self.assertEqual(resp.data.get("user_email"), "custom_staff@dtc.internal")
                mock_get_user.assert_not_called()


class CustomEmailValidatorTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_non_gmail_domain_rejected(self):
        invalid_domains = [
            "vijayzidane321@g.com",
            "vijayzidane321@gail.com",
            "user@yaho.com",
            "user@yahoo.com",
            "user@domain",
            "user@domain.123",
            "user@outlook.com",
            "user@hotmail.com",
        ]
        for email in invalid_domains:
            resp = self.client.post("/auth/send-otp/", {"email": email}, format="json")
            self.assertEqual(resp.status_code, 400, f"Expected 400 for {email}")
            self.assertEqual(resp.data.get("code"), "INVALID_EMAIL")
            self.assertEqual(resp.data.get("error"), "Only @gmail.com email addresses are supported.")
            self.assertNotIn("suggestion", resp.data)

    def test_consecutive_dots_rejected(self):
        resp = self.client.post("/auth/send-otp/", {"email": "user..name@gmail.com"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_EMAIL")
        self.assertIn("consecutive dots", resp.data.get("error"))

    def test_leading_or_trailing_dots_rejected(self):
        resp = self.client.post("/auth/send-otp/", {"email": ".username@gmail.com"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_EMAIL")
        self.assertIn("begin or end with a dot", resp.data.get("error"))

        resp = self.client.post("/auth/send-otp/", {"email": "username.@gmail.com"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_EMAIL")
        self.assertIn("begin or end with a dot", resp.data.get("error"))

    def test_username_length_rejected(self):
        resp = self.client.post("/auth/send-otp/", {"email": "ab@gmail.com"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_EMAIL")
        self.assertIn("between 3 and 64 characters", resp.data.get("error"))

    def test_valid_emails_pass(self):
        from coordinator.auth_views import validate_custom_email
        valid_emails = [
            "vijayzidane321@gmail.com",
            "test.user+tag@gmail.com",
            "first_last@gmail.com",
            "john.doe123@gmail.com",
        ]
        for email in valid_emails:
            is_valid, err = validate_custom_email(email)
            self.assertTrue(is_valid, f"Failed for {email}: {err}")
            self.assertIsNone(err)

