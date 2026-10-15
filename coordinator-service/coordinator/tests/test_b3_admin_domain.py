import os
from unittest.mock import patch
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory

from coordinator.views import _get_caller


class AdminDomainB3Tests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    @override_settings(IS_TESTING=False, DEBUG=False)
    def test_unset_or_empty_admin_domain_grants_nobody_staff_f18(self):
        """F-18: When ADMIN_DOMAIN is unset or empty, nobody is staff via email domain."""
        request = self.factory.get("/transactions/")
        for empty_val in [None, "", "   "]:
            env = {} if empty_val is None else {"ADMIN_DOMAIN": empty_val}
            with patch.dict(os.environ, env, clear=(empty_val is None)), \
                 patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "1", "user_email": "admin@dtc.internal"}):
                if empty_val is None:
                    os.environ.pop("ADMIN_DOMAIN", None)
                is_auth, uid, is_staff = _get_caller(request)
                self.assertTrue(is_auth)
                self.assertFalse(is_staff, f"ADMIN_DOMAIN={empty_val!r} should NOT grant staff!")

    @override_settings(IS_TESTING=False, DEBUG=False)
    def test_case_insensitive_admin_domain_matching_f18(self):
        """F-18: Admin domain matching must be case-insensitive."""
        request = self.factory.get("/transactions/")
        with patch.dict(os.environ, {"ADMIN_DOMAIN": "DTC.INTERNAL"}), \
             patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "1", "user_email": "alice@dtc.internal"}):
            is_auth, uid, is_staff = _get_caller(request)
            self.assertTrue(is_staff, "Mixed-case domain must match case-insensitively.")

        with patch.dict(os.environ, {"ADMIN_DOMAIN": "dtc.internal"}), \
             patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "1", "user_email": "ALICE@DTC.INTERNAL"}):
            is_auth, uid, is_staff = _get_caller(request)
            self.assertTrue(is_staff, "Uppercase email must match lowercase ADMIN_DOMAIN.")

    @override_settings(IS_TESTING=False, DEBUG=False)
    def test_enforce_at_boundary_reject_domain_spoofing_f18(self):
        """F-18: Suffix/subdomain spoofing must be rejected (enforcing exact @domain boundary)."""
        request = self.factory.get("/transactions/")
        spoofed_emails = [
            "attacker@notdtc.internal",
            "attacker@evil-dtc.internal",
            "attacker@dtc.internal.attacker.com",
            "attacker@fake_dtc.internal",
        ]
        with patch.dict(os.environ, {"ADMIN_DOMAIN": "dtc.internal"}):
            for email in spoofed_emails:
                with patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "1", "user_email": email}):
                    is_auth, uid, is_staff = _get_caller(request)
                    self.assertFalse(is_staff, f"Email {email} should NOT be recognized as staff for domain dtc.internal!")

    @override_settings(IS_TESTING=False, DEBUG=False)
    def test_admin_domain_with_leading_at_symbol_f18(self):
        """F-18: ADMIN_DOMAIN defined with leading @ (e.g. '@dtc.internal') should work correctly."""
        request = self.factory.get("/transactions/")
        with patch.dict(os.environ, {"ADMIN_DOMAIN": "@dtc.internal"}), \
             patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "1", "user_email": "alice@dtc.internal"}):
            is_auth, uid, is_staff = _get_caller(request)
            self.assertTrue(is_staff, "ADMIN_DOMAIN with leading @ should match email domain.")
