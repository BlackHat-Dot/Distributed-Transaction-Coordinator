import os
import sys
from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class PaymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "payments"

    def ready(self):
        is_testing = getattr(settings, "IS_TESTING", False)
        if not is_testing:
            key = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY")
            if not key or not str(key).strip():
                raise ImproperlyConfigured(
                    "CRITICAL SECURITY ERROR: INTERNAL_SERVICE_KEY environment variable is not set or empty in non-test environment."
                )
