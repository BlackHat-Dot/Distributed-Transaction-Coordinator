import os
from unittest.mock import patch
from django.test import TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory

from coordinator.views import _faults_on, _verify_staff_key


class FaultInjectionAndSecretsB4Tests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.client = APIClient()

    def test_verify_staff_key_rejects_empty_secrets_f19(self):
        """F-19: _verify_staff_key must reject empty, whitespace, or missing secrets."""
        with patch.dict(os.environ, {"STAFF_API_KEY": ""}):
            self.assertFalse(_verify_staff_key(""))
            self.assertFalse(_verify_staff_key("   "))
            self.assertFalse(_verify_staff_key("some_key"))

        with patch.dict(os.environ, {"STAFF_API_KEY": "secret123"}):
            self.assertFalse(_verify_staff_key(""))
            self.assertFalse(_verify_staff_key(None))
            self.assertFalse(_verify_staff_key("wrong"))
            self.assertTrue(_verify_staff_key("secret123"))

    @override_settings(DEBUG=False, ENABLE_FAULT_INJECTION=True)
    def test_fault_injection_strictly_denied_in_production_f24_f45(self):
        """F-24/F-45: Fault injection must return False in production (DEBUG=False) regardless of env flag."""
        self.assertFalse(_faults_on(), "Fault injection must be denied when DEBUG=False!")

    @override_settings(DEBUG=True, ENABLE_FAULT_INJECTION=False)
    def test_fault_injection_denied_in_debug_without_flag_f45(self):
        """F-45: When DEBUG=True but ENABLE_FAULT_INJECTION is False, fault injection is disallowed."""
        self.assertFalse(_faults_on(), "Fault injection must be denied without ENABLE_FAULT_INJECTION flag!")

    @override_settings(DEBUG=True, ENABLE_FAULT_INJECTION=True)
    def test_faults_on_with_debug_and_flag_f45(self):
        """F-45: When DEBUG=True and ENABLE_FAULT_INJECTION is True, fault injection is allowed."""
        self.assertTrue(_faults_on(), "Fault injection must be allowed when DEBUG=True and flag is True.")

    @override_settings(DEBUG=False, ENABLE_FAULT_INJECTION=False)
    def test_coordinator_crash_simulation_denied_in_production_f45(self):
        """F-45: In production, client sending 'coordinator: true' must not trigger crash simulation."""
        with patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "usr_1"}):
            resp = self.client.post(
                "/transactions/",
                {"seat_ids": [1], "action": "hold", "coordinator": True},
                format="json",
            )
            # Should be rejected with 400 INVALID_FIELDS because 'coordinator' is not allowed in production
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.data.get("code"), "INVALID_FIELDS")
