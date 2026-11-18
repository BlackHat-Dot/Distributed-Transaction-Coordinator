import os
import sys
from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


# Configures the Payments application lifecycle, setting default primary key types and enforcing startup invariants.
class PaymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "payments"

    def ready(self):
        # Bypasses strict credential requirements during automated test execution to support isolated test runners.
        is_testing = getattr(settings, "IS_TESTING", False)
        if is_testing:
            return

        # Whitelists administrative management commands (migrate, check, collectstatic) that run offline without serving HTTP traffic.
        non_serving_commands = {
            "migrate",
            "makemigrations",
            "collectstatic",
            "check",
            "test",
            "shell",
            "createsuperuser",
            "dumpdata",
            "loaddata",
            "showmigrations",
            "squashmigrations",
            "inspectdb",
            "flush",
            "sqlmigrate",
        }
        cmd = sys.argv[1] if len(sys.argv) > 1 else None
        if cmd in non_serving_commands:
            return

        key = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY")
        if not key or not str(key).strip():
            # Fail-fast security circuit breaker: halts startup immediately if INTERNAL_SERVICE_KEY is unset in live HTTP serving environments.
            raise ImproperlyConfigured(
                "CRITICAL SECURITY ERROR: INTERNAL_SERVICE_KEY environment variable is not set or empty in non-test environment."
            )
