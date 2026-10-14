from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIRequestFactory

from coordinator.auth_views import _create_jwt, heartbeat, logout, verify_otp


class TestG1RealHeartbeatPresence(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user_email = "g1user@gmail.com"
        self.user_id = "usr_g1_test_999"
        self.token = _create_jwt(user_id=self.user_id, user_email=self.user_email, session_id="sid_123")

    def test_real_heartbeat_writes_both_email_and_user_id_session_keys(self):
        """
        G1: Real heartbeat write path must write both dtc:session:{email} and dtc:session:{user_id}.
        """
        redis_store = {}

        class FakeRedis:
            def get(self, k):
                return redis_store.get(k)

            def setex(self, k, ttl, v):
                redis_store[k] = {"val": v, "ttl": ttl}

            def delete(self, k):
                redis_store.pop(k, None)

            def exists(self, k):
                return 1 if k in redis_store else 0

        fake_redis = FakeRedis()

        with patch("coordinator.auth_views._get_redis_client", return_value=fake_redis):
            req = self.factory.post(
                "/auth/session/heartbeat/",
                HTTP_AUTHORIZATION=f"Bearer {self.token}",
            )
            resp = heartbeat(req)
            self.assertEqual(resp.status_code, 200)

            # Assert real heartbeat wrote dtc:session:{user_id} and dtc:session:{email} with 30s TTL
            self.assertIn(f"dtc:session:{self.user_email}", redis_store)
            self.assertIn(f"dtc:session:{self.user_id}", redis_store)
            self.assertEqual(redis_store[f"dtc:session:{self.user_email}"]["ttl"], 30)
            self.assertEqual(redis_store[f"dtc:session:{self.user_id}"]["ttl"], 30)

            # Now test real logout clears both keys
            logout_req = self.factory.post(
                "/auth/logout/",
                HTTP_AUTHORIZATION=f"Bearer {self.token}",
            )
            logout_resp = logout(logout_req)
            self.assertEqual(logout_resp.status_code, 200)
            self.assertNotIn(f"dtc:session:{self.user_email}", redis_store)
            self.assertNotIn(f"dtc:session:{self.user_id}", redis_store)

    def test_verify_otp_writes_both_email_and_user_id_session_keys(self):
        """
        G1: Real verify_otp login path must write both dtc:session:{email} and dtc:session:{user_id}.
        """
        redis_store = {"dtc:otp:g1login@gmail.com": "123456"}

        class FakeRedis:
            def get(self, k):
                return redis_store.get(k)

            def setex(self, k, ttl, v):
                redis_store[k] = {"val": v, "ttl": ttl}

            def delete(self, k):
                redis_store.pop(k, None)

            def exists(self, k):
                return 1 if k in redis_store else 0

        fake_redis = FakeRedis()

        with patch("coordinator.auth_views._get_redis_client", return_value=fake_redis):
            req = self.factory.post(
                "/auth/verify-otp/",
                {"email": "g1login@gmail.com", "otp": "123456"},
                format="json",
            )
            resp = verify_otp(req)
            self.assertEqual(resp.status_code, 200)
            user_id = resp.data["user_id"]

            self.assertIn("dtc:session:g1login@gmail.com", redis_store)
            self.assertIn(f"dtc:session:{user_id}", redis_store)
            self.assertEqual(redis_store["dtc:session:g1login@gmail.com"]["ttl"], 30)
            self.assertEqual(redis_store[f"dtc:session:{user_id}"]["ttl"], 30)
