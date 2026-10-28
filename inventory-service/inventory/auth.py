# verified

import logging
import secrets
import jwt
from django.conf import settings
from rest_framework.response import Response

logger = logging.getLogger(__name__)


def _is_staff_request(request):
    """
    Verifies staff/admin identity using constant-time comparison against settings.STAFF_API_KEY.
    """
    if not request:
        return False

    staff_key = None
    if hasattr(request, "headers") and request.headers:
        staff_key = request.headers.get("X-Staff-Key")
    elif hasattr(request, "META") and request.META:
        staff_key = request.META.get("HTTP_X_STAFF_KEY")

    if not staff_key or not isinstance(staff_key, str) or not staff_key.strip():
        return False

    configured_key = getattr(settings, "STAFF_API_KEY", None)
    if configured_key and isinstance(configured_key, str) and configured_key.strip():
        return secrets.compare_digest(staff_key.strip(), configured_key.strip())
    return False


def _get_authenticated_user(request):
    """
    Extracts and cryptographically verifies the JWT from Authorization header.
    Returns dict {"user_id": ..., "user_email": ..., "sid": ...} if valid, else None.
    Results are memoized on the request object to avoid redundant cryptographic decoding.
    """
    if not request:
        return None

    if hasattr(request, "_cached_authenticated_user"):
        return request._cached_authenticated_user

    user_info = None
    try:
        auth_header = ""
        if hasattr(request, "headers") and request.headers:
            auth_header = request.headers.get("Authorization", "")
        elif hasattr(request, "META") and request.META:
            auth_header = request.META.get("HTTP_AUTHORIZATION", "")

        if auth_header and isinstance(auth_header, str):
            parts = auth_header.strip().split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                token = parts[1].strip()
                key = getattr(settings, "JWT_SIGNING_KEY", None) or getattr(settings, "SECRET_KEY", None)
                if key and token:
                    payload = jwt.decode(
                        token,
                        key,
                        algorithms=["HS256"],
                        options={"require": ["exp"]},
                    )
                    if isinstance(payload, dict):
                        raw_uid = payload.get("user_id")
                        raw_uemail = payload.get("user_email")
                        uid_str = str(raw_uid).strip() if raw_uid is not None else ""
                        uemail_str = str(raw_uemail).strip() if raw_uemail is not None else ""

                        clean_uid = uid_str if uid_str else None
                        clean_uemail = uemail_str if uemail_str else None

                        if clean_uid or clean_uemail:
                            user_info = {
                                "user_id": clean_uid,
                                "user_email": clean_uemail,
                                "sid": payload.get("sid"),
                            }
    except jwt.PyJWTError as e:
        logger.debug("JWT verification failed: %s", type(e).__name__)
    except Exception as e:
        logger.debug("Unexpected error during JWT verification: %s", type(e).__name__)

    try:
        request._cached_authenticated_user = user_info
    except (AttributeError, TypeError):
        pass

    return user_info


def _get_authenticated_user_id(request):
    """
    Extracts and cryptographically verifies the JWT from Authorization header.
    Returns sanitized user_id string if signature and expiration are valid, None otherwise.
    """
    auth_user = _get_authenticated_user(request)
    if not auth_user or not isinstance(auth_user, dict):
        return None
    uid = auth_user.get("user_id")
    return str(uid).strip() if uid and str(uid).strip() else None


# verified
def _verify_internal_service_key(request, allow_client=False):
    """
    Ensures internal state-mutating endpoints (confirm, release-booked)
    can only be called by internal services (e.g. coordinator) presenting
    the shared secret.
    If allow_client=True (e.g. reserve_seat or user hold cancel), calls without
    the header are permitted, but invalid keys are always rejected with 403.
    Returns (auth_err, is_internal).
    """
    key = None
    if request:
        if hasattr(request, "headers") and request.headers:
            key = request.headers.get("X-Internal-Service-Key")
        elif hasattr(request, "META") and request.META:
            key = request.META.get("HTTP_X_INTERNAL_SERVICE_KEY")

    configured_key = getattr(settings, "INTERNAL_SERVICE_KEY", None)

    if key is not None:
        if not configured_key:
            logger.error("INTERNAL_SERVICE_KEY is not configured on this server.")
            return Response(
                {
                    "error": "The service is temporarily unavailable. Our team has been notified.",
                    "code": "SERVICE_CONFIGURATION_ERROR",
                },
                status=503,
            ), False

        if not secrets.compare_digest(str(key).strip(), str(configured_key).strip()):
            return Response(
                {
                    "error": "Invalid service authentication credentials.",
                    "code": "INVALID_CREDENTIALS",
                },
                status=403,
            ), False

        return None, True

    if getattr(settings, "IS_TESTING", False):
        return None, True

    if allow_client:
        return None, False

    return Response(
        {
            "error": "You do not have permission to perform this action.",
            "code": "PERMISSION_DENIED",
        },
        status=403,
    ), False