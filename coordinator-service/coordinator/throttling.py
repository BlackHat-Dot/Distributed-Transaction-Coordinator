import math

from rest_framework.exceptions import NotAuthenticated, PermissionDenied, Throttled
from rest_framework.permissions import BasePermission
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import exception_handler

from coordinator.auth_views import _get_caller


AUTH_MSG = "Authentication required. Please sign in to book seats."


class IsBookingAuthenticated(BasePermission):
    def has_permission(self, request, view):
        if not _get_caller(request).is_authenticated:
            raise NotAuthenticated(detail=AUTH_MSG)
        return True


class BookingRateThrottle(SimpleRateThrottle):
    """
    Rate limits POST requests per authenticated user.
    The rate comes from DEFAULT_THROTTLE_RATES["coordinator_transactions"].
    Attach to transaction views together with IsBookingAuthenticated.
    """
    scope = "coordinator_transactions"

    def get_cache_key(self, request, view):
        if request.method != "POST":  # only limit writes
            return None

        caller = _get_caller(request)
        if caller.is_staff:  # staff skip throttling
            return None

        return self.cache_format % {"scope": self.scope, "ident": f"user_{caller.user_id}"}


def coordinator_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None

    if isinstance(exc, Throttled):
        wait = max(1, math.ceil(exc.wait)) if exc.wait is not None else 60
        response.data = {
            "error": f"Request limit exceeded. Try again in {wait} seconds.",
            "code": "THROTTLED",
            "wait": wait,
        }
    elif isinstance(exc, (NotAuthenticated, PermissionDenied)):
        caller = _get_caller(context["request"])
        if not caller.is_authenticated:
            response.status_code = 401
            response.data = {"error": AUTH_MSG, "code": "AUTHENTICATION_REQUIRED"}
    return response