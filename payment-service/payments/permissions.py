# verified

import os
import secrets
from django.conf import settings
from rest_framework.permissions import BasePermission
from rest_framework.response import Response


def _get_valid_internal_keys():
    """
    Dynamically loads valid internal keys from Django settings with OS environment fallback.
    Returns stripped, non-empty secret keys. Fails closed if none configured.
    """
    keys = []
    for attr in ("INTERNAL_SERVICE_KEY", "INTERNAL_SERVICE_KEY_FALLBACK"):
        val = getattr(settings, attr, None)
        if val is None:
            val = os.getenv(attr)
        if val is not None and isinstance(val, str) and val.strip():
            keys.append(val.strip())
    return keys


def verify_internal_service_key(request):
    """
    Ensures internal payment endpoints (charge, check, refund, void) can only be called
    by internal services presenting the shared secret.
    Uses constant-time comparison across all configured keys to prevent timing side-channels (SEC-04).
    Fails closed if the secret is missing, empty, or unconfigured.
    """
    if not request:
        return Response(
            {"error": "Forbidden: missing internal service key."},
            status=403,
        )

    valid_keys = _get_valid_internal_keys()
    if not valid_keys:
        return Response(
            {"error": "Forbidden: missing internal service key."},
            status=403,
        )

    key = None
    if hasattr(request, "headers") and request.headers:
        key = request.headers.get("X-Internal-Service-Key")
    if key is None and hasattr(request, "META") and request.META:
        key = request.META.get("HTTP_X_INTERNAL_SERVICE_KEY")

    if not key or not isinstance(key, str) or not key.strip():
        return Response(
            {"error": "Forbidden: missing internal service key."},
            status=403,
        )

    try:
        key_bytes = key.encode("utf-8")
    except Exception:
        return Response(
            {"error": "Forbidden: invalid internal service key."},
            status=403,
        )

    matched = False
    for expected in valid_keys:
        try:
            expected_bytes = expected.encode("utf-8")
            if secrets.compare_digest(key_bytes, expected_bytes):
                matched = True
        except Exception:
            continue

    if not matched:
        return Response(
            {"error": "Forbidden: invalid internal service key."},
            status=403,
        )

    return None


class InternalServicePermission(BasePermission):
    """
    DRF permission class for internal microservice communication.
    Propagates exact security error messages upon rejection.
    """
    message = "Forbidden: invalid internal service key."

    def has_permission(self, request, view):
        err = verify_internal_service_key(request)
        if err is not None:
            if isinstance(err.data, dict) and "error" in err.data:
                self.message = err.data["error"]
            return False
        return True