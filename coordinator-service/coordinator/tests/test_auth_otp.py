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

        # 5th wrong attempt must trigger 429 and ACCOUNT_LOCKED
        ver_resp5 = self.client.post("/auth/verify-otp/", {"email": self.email, "otp": "999999"}, format="json")
        self.assertEqual(ver_resp5.status_code, 429)
        self.assertEqual(ver_resp5.data.get("code"), "ACCOUNT_LOCKED")

        # Further verification attempts while locked return 429
        ver_resp_locked = self.client.post("/auth/verify-otp/", {"email": self.email, "otp": "123456"}, format="json")
        self.assertEqual(ver_resp_locked.status_code, 429)
        self.assertEqual(ver_resp_locked.data.get("code"), "ACCOUNT_LOCKED")

        # Requesting new OTP while locked must also be rejected with 429
        req_locked = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
        self.assertEqual(req_locked.status_code, 429)
        self.assertEqual(req_locked.data.get("code"), "ACCOUNT_LOCKED")
