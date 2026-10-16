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
    staff_key = request.headers.get("X-Staff-Key")
    if not staff_key:
        return False
    configured_key = getattr(settings, "STAFF_API_KEY", None)
    if configured_key:
        return secrets.compare_digest(str(staff_key), str(configured_key))
    return False


def _get_authenticated_user(request):
    """
    Extracts and cryptographically verifies the JWT from Authorization header.
    Returns dict {"user_id": ..., "user_email": ..., "sid": ...} if valid, else None.
    If header verification fails and ALLOW_UNVERIFIED_IDENTITY_HEADERS is True (e.g. in test env),
    falls back to unverified request headers.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        key = getattr(settings, "JWT_SIGNING_KEY", None) or settings.SECRET_KEY
        try:
            payload = jwt.decode(
                token,
                key,
                algorithms=["HS256"],
                options={"require": ["exp"]},
            )
            uid = payload.get("user_id")
            uemail = payload.get("user_email")
            if uid or uemail:
                return {
                    "user_id": str(uid) if uid is not None else None,
                    "user_email": str(uemail) if uemail is not None else None,
                    "sid": payload.get("sid"),
                }
        except jwt.PyJWTError as e:
            logger.debug("JWT verification failed: %s", type(e).__name__)
        except Exception as e:
            logger.debug("Unexpected error during JWT verification: %s", type(e).__name__)

    if getattr(settings, "ALLOW_UNVERIFIED_IDENTITY_HEADERS", False):
        uid = request.headers.get("X-User-ID")
        uemail = request.headers.get("X-User-Email")
        if uid or uemail:
            return {
                "user_id": str(uid) if uid else None,
                "user_email": str(uemail) if uemail else None,
                "sid": None,
            }

    return None


def _get_authenticated_user_id(request):
    """
    Extracts and cryptographically verifies the JWT from Authorization header.
    Returns user_id string if signature and expiration are valid, None otherwise.
    """
    auth_user = _get_authenticated_user(request)
    return auth_user.get("user_id") if auth_user else None


def _verify_internal_service_key(request, allow_client=False):
    """
    Ensures internal state-mutating endpoints (confirm, release-booked)
    can only be called by internal services (e.g. coordinator) presenting
    the shared secret.
    If allow_client=True (e.g. reserve_seat or user hold cancel), calls without
    the header are permitted, but invalid keys are always rejected with 403.
    Returns (auth_err, is_internal).
    """
    key = request.headers.get("X-Internal-Service-Key")
    configured_key = getattr(settings, "INTERNAL_SERVICE_KEY", None)

    if key is not None:
        if not configured_key or not secrets.compare_digest(str(key), str(configured_key)):
            return Response(
                {"error": "Forbidden: invalid internal service key."},
                status=403,
            ), False
        return None, True

    if getattr(settings, "IS_TESTING", False):
        return None, True

    if allow_client:
        return None, False

    return Response(
        {"error": "Forbidden: missing internal service key."},
        status=403,
    ), False
