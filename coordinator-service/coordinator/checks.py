# Provides access to operating system environment variables (such as service keys and domains) configured on the host or container.
import os

# Imports Django's runtime settings module to access configured secrets and active execution environment flags.
from django.conf import settings

# Imports Django's system check framework utilities: Warning creates non-fatal diagnostic alerts, register connects custom checks, and Tags groups them by topic.
from django.core.checks import Warning, register, Tags


# Hooks this validation function into Django's startup check framework under the 'security' category so it executes during manage.py check or runserver.
@register(Tags.security)
# Defines a custom security diagnostic check to ensure all vital service authentication secrets and domains are safely configured before booting.
def check_service_keys_and_admin_domain(app_configs, **kwargs):
    """
    H14: System check to validate that INTERNAL_SERVICE_KEY, STAFF_API_KEY,
    and ADMIN_DOMAIN are properly configured in non-testing environments.
    """
    # Initializes an empty list to collect any security configuration warnings encountered during inspection.
    warnings = []

    # Checks if the server is currently executing inside automated test runner mode where mock or dummy secrets are intentionally permitted.
    is_testing = getattr(settings, "IS_TESTING", False)

    # Immediately exits without raising security warnings when running tests to prevent test suites and CI pipelines from failing on absent production keys.
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
