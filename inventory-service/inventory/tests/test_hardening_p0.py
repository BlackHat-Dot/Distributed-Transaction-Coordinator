import time
import jwt
from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory

from inventory.views import _get_authenticated_user


class TestA1JWTVerification(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.secret = "test-secret-key-123"

    @override_settings(SECRET_KEY="test-secret-key-123")
    def test_a1_valid_token_with_secret_key(self):
        now = int(time.time())
        token = jwt.encode(
            {"user_id": "usr_99", "user_email": "fan@example.com", "sid": "sid_123", "exp": now + 3600},
            self.secret,
            algorithm="HS256",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNotNone(user)
        self.assertEqual(user, {"user_id": "usr_99", "user_email": "fan@example.com", "sid": "sid_123"})

    @override_settings(SECRET_KEY="test-secret-key-123", JWT_SIGNING_KEY="custom-jwt-key-456")
    def test_a1_valid_token_with_jwt_signing_key(self):
        now = int(time.time())
        token = jwt.encode(
            {"user_id": "usr_custom", "user_email": "custom@example.com", "sid": "sid_custom", "exp": now + 3600},
            "custom-jwt-key-456",
            algorithm="HS256",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNotNone(user)
        self.assertEqual(user, {"user_id": "usr_custom", "user_email": "custom@example.com", "sid": "sid_custom"})

    @override_settings(SECRET_KEY="test-secret-key-123")
    def test_a1_missing_exp_rejected(self):
        # Tokens without exp must be rejected
        token = jwt.encode(
            {"user_id": "usr_no_exp", "user_email": "noexp@example.com", "sid": "sid_no_exp"},
            self.secret,
            algorithm="HS256",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNone(user, "Tokens without exp claim must be rejected")

    @override_settings(SECRET_KEY="test-secret-key-123")
    def test_a1_expired_token_rejected(self):
        now = int(time.time())
        token = jwt.encode(
            {"user_id": "usr_exp", "user_email": "exp@example.com", "exp": now - 100},
            self.secret,
            algorithm="HS256",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNone(user)

    @override_settings(SECRET_KEY="test-secret-key-123")
    def test_a1_wrong_alg_none_rejected(self):
        # Algorithm "none" or without signature must be rejected
        now = int(time.time())
        token = jwt.encode(
            {"user_id": "usr_none", "user_email": "none@example.com", "exp": now + 3600},
            key="",
            algorithm="none",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNone(user)

    @override_settings(SECRET_KEY="test-secret-key-123")
    def test_a1_wrong_alg_hs512_rejected(self):
        now = int(time.time())
        token = jwt.encode(
            {"user_id": "usr_hs512", "user_email": "hs512@example.com", "exp": now + 3600},
            self.secret,
            algorithm="HS512",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNone(user)

    @override_settings(SECRET_KEY="test-secret-key-123")
    def test_a1_bad_signature_rejected(self):
        now = int(time.time())
        token = jwt.encode(
            {"user_id": "usr_bad", "user_email": "bad@example.com", "exp": now + 3600},
            "wrong-secret-key",
            algorithm="HS256",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNone(user)


class TestA2StaffAndInternalKeySettings(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    @override_settings(STAFF_API_KEY="dynamic-staff-key-888")
    def test_a2_staff_key_read_from_settings_at_call_time(self):
        from inventory.views import _is_staff_request
        req = self.factory.get("/test/", HTTP_X_STAFF_KEY="dynamic-staff-key-888")
        self.assertTrue(_is_staff_request(req))

    @override_settings(STAFF_API_KEY="", IS_TESTING=True, DEBUG=True)
    def test_a2_no_hardcoded_staff_key_fallback(self):
        # Even with DEBUG=True and IS_TESTING=True, the fallback secret 'dtc-staff-secret-key-2026' must NOT work
        from inventory.views import _is_staff_request
        req = self.factory.get("/test/", HTTP_X_STAFF_KEY="dtc-staff-secret-key-2026")
        self.assertFalse(_is_staff_request(req), "Hardcoded fallback secret must be removed")

    @override_settings(INTERNAL_SERVICE_KEY="dynamic-internal-key-999", IS_TESTING=False)
    def test_a2_internal_key_read_from_settings_at_call_time(self):
        from inventory.views import _verify_internal_service_key
        req = self.factory.get("/test/", HTTP_X_INTERNAL_SERVICE_KEY="dynamic-internal-key-999")
        err, is_internal = _verify_internal_service_key(req)
        self.assertIsNone(err)
        self.assertTrue(is_internal)

