import sys
from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security)
def check_bypass_flags(app_configs, **kwargs):
    errors = []
    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    if cmd == "test":
        return errors

    if not getattr(settings, "DEBUG", False):
        if getattr(settings, "IS_TESTING", False):
            errors.append(
                Error(
                    "Security hazard: IS_TESTING cannot be True when DEBUG is False.",
                    hint="Set IS_TESTING to False in production.",
                    id="inventory.E001",
                )
            )
        if getattr(settings, "ALLOW_UNVERIFIED_IDENTITY_HEADERS", False):
            errors.append(
                Error(
                    "Security hazard: ALLOW_UNVERIFIED_IDENTITY_HEADERS cannot be True when DEBUG is False.",
                    hint="Set ALLOW_UNVERIFIED_IDENTITY_HEADERS to False in production.",
                    id="inventory.E002",
                )
            )
        if getattr(settings, "ALLOW_UNAUTHENTICATED_RESET", False):
            errors.append(
                Error(
                    "Security hazard: ALLOW_UNAUTHENTICATED_RESET cannot be True when DEBUG is False.",
                    hint="Set ALLOW_UNAUTHENTICATED_RESET to False in production.",
                    id="inventory.E003",
                )
            )
    return errors
