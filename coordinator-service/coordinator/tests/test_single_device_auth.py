from django.test import TestCase
from django.core.cache import cache
from rest_framework.test import APIClient
from coordinator.auth_views import _get_redis_client


class SingleDeviceAuthTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.email = "vijay@gmail.com"
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

    def test_single_device_login_blocks_second_device(self):
        # 1. Device 1 requests and verifies OTP
        req1 = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
        self.assertEqual(req1.status_code, 200)

        otp1 = cache.get(f"dtc:otp:{self.email}")
        if not otp1 and self.redis_client:
            try:
                otp1 = self.redis_client.get(f"dtc:otp:{self.email}")
                if isinstance(otp1, bytes):
                    otp1 = otp1.decode("utf-8")
            except Exception:
                pass
        self.assertTrue(bool(otp1))

        ver1 = self.client.post("/auth/verify-otp/", {"email": self.email, "otp": otp1}, format="json")
        self.assertEqual(ver1.status_code, 200)
        self.assertTrue(ver1.data.get("success"))
        token1 = ver1.data.get("token")
        self.assertTrue(bool(token1))

        # Session is now active for Device 1
        # 2. Device 2 attempts to send OTP with same email -> must be BLOCKED with 409
        req2 = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
        self.assertEqual(req2.status_code, 409)
        self.assertEqual(req2.data.get("code"), "CONCURRENT_LOGIN_BLOCKED")
        self.assertIn("Another device is currently logged in", req2.data.get("error"))

        # 3. Device 2 attempts to verify OTP -> must also be BLOCKED with 409
        ver2 = self.client.post("/auth/verify-otp/", {"email": self.email, "otp": "123456"}, format="json")
        self.assertEqual(ver2.status_code, 409)
        self.assertEqual(ver2.data.get("code"), "CONCURRENT_LOGIN_BLOCKED")

        # 4. Device 1 sends heartbeat -> should succeed (200 OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token1}")
        hb_resp = self.client.post("/auth/heartbeat/")
        self.assertEqual(hb_resp.status_code, 200)
        self.assertEqual(hb_resp.data.get("status"), "alive")

        # 5. Device 1 logs out
        logout_resp = self.client.post("/auth/logout/")
        self.assertEqual(logout_resp.status_code, 200)

        # 6. Now Device 2 can log in without being blocked!
        self.client.credentials()  # Clear auth headers
        req3 = self.client.post("/auth/send-otp/", {"email": self.email}, format="json")
        self.assertEqual(req3.status_code, 200)
