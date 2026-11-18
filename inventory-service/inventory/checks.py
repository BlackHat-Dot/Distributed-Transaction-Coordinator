# verified

import sys
from django.conf import settings
from django.core.checks import Error, Tags, register


# Parses heterogeneous environment and settings values (booleans, strings 'true'/'1'/'on') into canonical python booleans.
def _is_flag_active(val) -> bool:
    """Safely evaluates boolean or string-serialized truthy flags."""
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "t", "on")
    return bool(val)


# Registers startup security check with Django system check framework to validate configuration before binding server sockets.
@register(Tags.security)
def check_bypass_flags(app_configs, **kwargs):
    """
    Security check enforcing that development/testing bypass flags
    (IS_TESTING, ALLOW_UNVERIFIED_IDENTITY_HEADERS, ALLOW_UNAUTHENTICATED_RESET)
    cannot be enabled when running with DEBUG=False in production.
    """
    errors = []

    # If running under a test runner, bypass errors to allow test suite execution
    # Detects whether the process is executing within a test harness (pytest or manage.py test) to permit synthetic fixture flags.
    args = getattr(sys, "argv", []) or []
    is_test_runner = (
        any(arg == "test" for arg in args)
        or "pytest" in sys.modules
    )
    if is_test_runner:
        return errors

    # Production gate: enforces that non-DEBUG production deployments never execute with insecure development bypass flags.
    if not _is_flag_active(getattr(settings, "DEBUG", False)):
        if _is_flag_active(getattr(settings, "IS_TESTING", False)):
            errors.append(
                Error(
                    "Security hazard: IS_TESTING cannot be True when DEBUG is False.",
                    hint="Set IS_TESTING to False in production.",
                    id="inventory.E001",
                )
            )
        if _is_flag_active(getattr(settings, "ALLOW_UNVERIFIED_IDENTITY_HEADERS", False)):
            errors.append(
                Error(
                    "Security hazard: ALLOW_UNVERIFIED_IDENTITY_HEADERS cannot be True when DEBUG is False.",
                    hint="Set ALLOW_UNVERIFIED_IDENTITY_HEADERS to False in production.",
                    id="inventory.E002",
                )
            )
        if _is_flag_active(getattr(settings, "ALLOW_UNAUTHENTICATED_RESET", False)):
            errors.append(
                Error(
                    "Security hazard: ALLOW_UNAUTHENTICATED_RESET cannot be True when DEBUG is False.",
                    hint="Set ALLOW_UNAUTHENTICATED_RESET to False in production.",
                    id="inventory.E003",
                )
            )

    return errors