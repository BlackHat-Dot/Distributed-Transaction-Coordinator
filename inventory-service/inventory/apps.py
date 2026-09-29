import sys
from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class InventoryConfig(AppConfig):
    name = 'inventory'

    def ready(self):
        import inventory.checks  # noqa: F401

        # Enforce startup security whenever DEBUG is False unless running the Django test runner.
        # This covers all server runtimes (gunicorn, uvicorn, daphne, celery) and management commands (migrate, etc.).
        is_test_runner = (len(sys.argv) > 1 and sys.argv[1] == "test") or "pytest" in sys.modules

        if not getattr(settings, "DEBUG", False) and not is_test_runner:
            if any([
                getattr(settings, "IS_TESTING", False),
                getattr(settings, "ALLOW_UNVERIFIED_IDENTITY_HEADERS", False),
                getattr(settings, "ALLOW_UNAUTHENTICATED_RESET", False),
            ]):
                raise ImproperlyConfigured(
                    "Security hazard: Testing bypass flags (IS_TESTING, ALLOW_UNVERIFIED_IDENTITY_HEADERS, "
                    "ALLOW_UNAUTHENTICATED_RESET) cannot be True when DEBUG is False."
                )
