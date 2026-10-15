import os
import runpy
from pathlib import Path
from unittest import TestCase



class TestG3SettingsAudit(TestCase):
    def setUp(self):
        self.settings_path = Path(__file__).resolve().parent.parent.parent / "inventory_service" / "settings.py"

    def _exec_settings(self, env_vars):
        """Execute inventory_service/settings.py in an isolated environment."""
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env_vars)
            # Ensure sys.argv does not contain 'test' when simulating non-test execution
            import sys
            old_argv = sys.argv
            sys.argv = ["manage.py", "runserver"]
            try:
                return runpy.run_path(str(self.settings_path))
            finally:
                sys.argv = old_argv
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_g3_production_secret_key_fails_if_unset(self):
        env = {
            "DEBUG": "False",
            "ALLOWED_HOSTS": "example.com",
            "INTERNAL_SERVICE_KEY": "valid-internal-key-12345",
        }
        with self.assertRaises(RuntimeError) as cm:
            self._exec_settings(env)
        self.assertIn("SECRET_KEY environment variable is not set", str(cm.exception))

    def test_g3_production_secret_key_fails_if_insecure_default(self):
        env = {
            "DEBUG": "False",
            "ALLOWED_HOSTS": "example.com",
            "SECRET_KEY": "django-insecure-test",
            "INTERNAL_SERVICE_KEY": "valid-internal-key-12345",
        }
        with self.assertRaises(RuntimeError) as cm:
            self._exec_settings(env)
        self.assertIn("Insecure default SECRET_KEY detected", str(cm.exception))

    def test_g3_production_internal_key_fails_if_unset(self):
        env = {
            "DEBUG": "False",
            "ALLOWED_HOSTS": "example.com",
            "SECRET_KEY": "valid-prod-secret-key-1234567890",
        }
        with self.assertRaises(RuntimeError) as cm:
            self._exec_settings(env)
        self.assertIn("INTERNAL_SERVICE_KEY environment variable is not set", str(cm.exception))

    def test_g3_production_internal_key_fails_if_hardcoded_default(self):
        env = {
            "DEBUG": "False",
            "ALLOWED_HOSTS": "example.com",
            "SECRET_KEY": "valid-prod-secret-key-1234567890",
            "INTERNAL_SERVICE_KEY": "dtc-internal-coordinator-secret-key-2026",
        }
        with self.assertRaises(RuntimeError) as cm:
            self._exec_settings(env)
        self.assertIn("Insecure default INTERNAL_SERVICE_KEY detected", str(cm.exception))

    def test_g3_production_staff_key_fails_if_hardcoded_default(self):
        env = {
            "DEBUG": "False",
            "ALLOWED_HOSTS": "example.com",
            "SECRET_KEY": "valid-prod-secret-key-1234567890",
            "INTERNAL_SERVICE_KEY": "valid-prod-internal-key-12345",
            "STAFF_API_KEY": "dtc-staff-secret-key-2026",
        }
        with self.assertRaises(RuntimeError) as cm:
            self._exec_settings(env)
        self.assertIn("Insecure default STAFF_API_KEY detected", str(cm.exception))

    def test_g3_production_staff_key_and_jwt_key_have_no_silent_fallback_when_unset(self):
        env = {
            "DEBUG": "False",
            "ALLOWED_HOSTS": "example.com",
            "SECRET_KEY": "valid-prod-secret-key-1234567890",
            "INTERNAL_SERVICE_KEY": "valid-prod-internal-key-12345",
            "DB_PASSWORD": "secret-prod-password",
        }

        glob = self._exec_settings(env)
        # When unset outside DEBUG, STAFF_API_KEY and JWT_SIGNING_KEY must be None, NOT fallback strings
        self.assertIsNone(glob.get("STAFF_API_KEY"), "STAFF_API_KEY must not have a silent fallback in production")
        self.assertIsNone(glob.get("JWT_SIGNING_KEY"), "JWT_SIGNING_KEY must not have a silent fallback in production")
