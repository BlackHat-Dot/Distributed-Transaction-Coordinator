import os
import secrets
from django.conf import settings
from rest_framework.permissions import BasePermission
from rest_framework.response import Response


def _get_valid_internal_keys():
    primary_key = getattr(settings, "INTERNAL_SERVICE_KEY", None)
    fallback_key = getattr(settings, "INTERNAL_SERVICE_KEY_FALLBACK", None) or os.getenv("INTERNAL_SERVICE_KEY_FALLBACK")
    return [k.strip() for k in (primary_key, fallback_key) if k and isinstance(k, str) and k.strip()]


def verify_internal_service_key(request):
    """
    Ensures internal payment endpoints (charge, check, refund) can only be called
    by internal services presenting the shared secret.
    Uses constant-time comparison on UTF-8 bytes to prevent timing side-channels (SEC-04).
    Fails closed if the secret is missing or empty.
    """
    valid_keys = _get_valid_internal_keys()
    if not valid_keys:
        return Response(
            {"error": "Forbidden: missing internal service key."},
            status=403,
        )

    key = request.headers.get("X-Internal-Service-Key")
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
                break
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
    """
    def has_permission(self, request, view):
        return verify_internal_service_key(request) is None
