# verified

import logging
import math

from rest_framework.exceptions import NotAuthenticated, PermissionDenied, Throttled
from rest_framework.permissions import BasePermission
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import exception_handler

from coordinator.auth_views import _get_caller, _get_client_ip

logger = logging.getLogger(__name__)

AUTH_MSG = "Authentication required. Please sign in to book seats."


class SafeRateThrottle(SimpleRateThrottle):
    """
    Base throttle that safely identifies clients using verified IP
    instead of spoofable raw X-Forwarded-For headers.
    """
    def get_ident(self, request):
        try:
            ip = _get_client_ip(request)
            if ip and ip != "unknown":
                return ip
        except Exception as exc:
            logger.warning("Failed to resolve client IP in SafeRateThrottle: %s", exc)

        meta = getattr(request, "META", None)
        if meta and hasattr(meta, "get"):
            remote_addr = meta.get("REMOTE_ADDR")
            if isinstance(remote_addr, str) and remote_addr.strip():
                return remote_addr.strip()
        return "unknown"


class IsBookingAuthenticated(BasePermission):
    def has_permission(self, request, view):
        caller = _get_caller(request)
        if not caller or not caller.is_authenticated:
            raise NotAuthenticated(detail=AUTH_MSG)
        return True


class BookingRateThrottle(SafeRateThrottle):
    """
    Rate limits POST requests per authenticated user.
    Falls back to client IP for unauthenticated callers to prevent cache collision DoS.
    The rate comes from DEFAULT_THROTTLE_RATES["coordinator_transactions"].
    Attach to transaction views together with IsBookingAuthenticated.
    """
    scope = "coordinator_transactions"

    def get_cache_key(self, request, view):
        if request.method != "POST":  # only limit writes
            return None

        caller = _get_caller(request)
        if caller and caller.is_staff:  # staff skip throttling
            return None

        if caller and caller.is_authenticated and caller.user_id:
            ident = f"user_{caller.user_id}"
        else:
            ident = f"ip_{self.get_ident(request)}"

        return self.cache_format % {"scope": self.scope, "ident": ident}


class TransactionReadRateThrottle(SafeRateThrottle):
    """
    Rate limits GET (fetch) requests per authenticated user to prevent DB DoS/polling spam.
    Falls back to client IP for unauthenticated callers to prevent cache collision DoS.
    The rate comes from DEFAULT_THROTTLE_RATES["coordinator_transaction_reads"] (10/min).
    Attach to transaction views together with IsBookingAuthenticated.
    """
    scope = "coordinator_transaction_reads"

    def get_cache_key(self, request, view):
        if request.method != "GET":
            return None

        caller = _get_caller(request)
        if caller and caller.is_staff:  # staff skip throttling
            return None

        if caller and caller.is_authenticated and caller.user_id:
            ident = f"user_{caller.user_id}"
        else:
            ident = f"ip_{self.get_ident(request)}"

        return self.cache_format % {"scope": self.scope, "ident": ident}


class RecoveryStatusRateThrottle(SafeRateThrottle):
    """
    Rate limits recovery status requests to 1/min per verified client IP to prevent polling spam.
    """
    scope = "coordinator_recovery_status"
    rate = "1/min"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


def coordinator_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None

    if isinstance(exc, Throttled):
        try:
            wait = max(1, math.ceil(float(exc.wait))) if exc.wait is not None else 60
        except (TypeError, ValueError, OverflowError):
            wait = 60

        req = context.get("request") if isinstance(context, dict) else None
        if req and getattr(req, "method", None) == "GET":
            error_msg = f"Something went wrong. Please wait for {wait} seconds."
        else:
            error_msg = f"Request limit exceeded. Try again in {wait} seconds."
        response.data = {
            "error": error_msg,
            "code": "THROTTLED",
            "wait": wait,
        }
    elif isinstance(exc, (NotAuthenticated, PermissionDenied)):
        req = context.get("request") if isinstance(context, dict) else None
        if req:
            caller = _get_caller(req)
            if not caller or not caller.is_authenticated:
                response.status_code = 401
                response.data = {"error": AUTH_MSG, "code": "AUTHENTICATION_REQUIRED"}
        else:
            if isinstance(exc, NotAuthenticated):
                response.status_code = 401
                response.data = {"error": AUTH_MSG, "code": "AUTHENTICATION_REQUIRED"}
    return response