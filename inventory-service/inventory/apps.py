import sys
from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class InventoryConfig(AppConfig):
    name = 'inventory'

    def ready(self):
        import inventory.checks  # noqa: F401

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

        if not getattr(settings, "DEBUG", False):
            if any([
                getattr(settings, "IS_TESTING", False),
                getattr(settings, "ALLOW_UNVERIFIED_IDENTITY_HEADERS", False),
                getattr(settings, "ALLOW_UNAUTHENTICATED_RESET", False),
            ]):
                raise ImproperlyConfigured(
                    "Security hazard: Testing bypass flags (IS_TESTING, ALLOW_UNVERIFIED_IDENTITY_HEADERS, "
                    "ALLOW_UNAUTHENTICATED_RESET) cannot be True when DEBUG is False."
                )
