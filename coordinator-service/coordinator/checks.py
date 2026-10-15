import os
from django.conf import settings
from django.core.checks import Warning, register, Tags


@register(Tags.security)
def check_service_keys_and_admin_domain(app_configs, **kwargs):
    """
    H14: System check to validate that INTERNAL_SERVICE_KEY, STAFF_API_KEY,
    and ADMIN_DOMAIN are properly configured in non-testing environments.
    """
    warnings = []
    is_testing = getattr(settings, "IS_TESTING", False)
    if is_testing:
        return warnings

    internal_key = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY")
    if not internal_key or internal_key == "dtc-internal-coordinator-secret-key-2026":
        warnings.append(
            Warning(
                "INTERNAL_SERVICE_KEY is unset or using insecure default.",
                hint="Set a unique, strong INTERNAL_SERVICE_KEY in your environment.",
                id="coordinator.W001",
            )
        )

    staff_key = os.getenv("STAFF_API_KEY")
    if not staff_key:
        warnings.append(
            Warning(
                "STAFF_API_KEY environment variable is unset.",
                hint="Set STAFF_API_KEY to enable secure staff and administrative operations.",
                id="coordinator.W002",
            )
        )

    admin_domain = os.getenv("ADMIN_DOMAIN")
    if not admin_domain:
        warnings.append(
            Warning(
                "ADMIN_DOMAIN environment variable is unset.",
                hint="Set ADMIN_DOMAIN (e.g. 'dtc.internal') to configure staff email domain matching.",
                id="coordinator.W003",
            )
        )

    return warnings
