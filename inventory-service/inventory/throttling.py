import math
import secrets

from django.conf import settings
from rest_framework.exceptions import Throttled
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import exception_handler

from .auth import _get_authenticated_user_id


def inventory_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if isinstance(exc, Throttled) and response is not None:
        wait = max(1, math.ceil(exc.wait)) if getattr(exc, "wait", None) is not None else 60
        response.data = {
            "error": f"Something went wrong. Please wait for {wait} seconds.",
            "code": "THROTTLED",
            "wait": wait,
            "available_in_seconds": wait,
        }
    return response


def _is_exempt_from_throttling(request):
    """
    Exempts internal services and staff with constant-time secret comparison against configured values.
    Never exempts based on header presence alone.
    """
    staff_key = request.headers.get("X-Staff-Key")
    configured_staff_key = getattr(settings, "STAFF_API_KEY", None)
    if staff_key and configured_staff_key and secrets.compare_digest(str(staff_key), str(configured_staff_key)):
        return True

    internal_key = request.headers.get("X-Internal-Service-Key")
    configured_internal_key = getattr(settings, "INTERNAL_SERVICE_KEY", None)
    if internal_key and configured_internal_key and secrets.compare_digest(str(internal_key), str(configured_internal_key)):
        return True

    return False


def _get_session_id(request):
    """Extract persistent anonymous session ID from X-Session-ID header or session cookie."""
    session_id = request.headers.get("X-Session-ID")
    if session_id and isinstance(session_id, str) and session_id.strip():
        return session_id.strip()
    if hasattr(request, "session") and request.session.session_key:
        return request.session.session_key
    return None


class BaseInventoryThrottle(SimpleRateThrottle):
    fallback_scope = None

    def get_rate(self):
        rf_settings = getattr(settings, "REST_FRAMEWORK", {}) or {}
        rates = rf_settings.get("DEFAULT_THROTTLE_RATES", {}) or {}
        if self.scope in rates:
            return rates[self.scope]
        if self.fallback_scope and self.fallback_scope in rates:
            return rates[self.fallback_scope]
        return super().get_rate()

    def allow_request(self, request, view):
        if _is_exempt_from_throttling(request):
            return True
        return super().allow_request(request, view)


class UserActionRateThrottle(BaseInventoryThrottle):
    scope = "inventory_user"

    def get_cache_key(self, request, view):
        uid = _get_authenticated_user_id(request)
        if not uid and hasattr(request, "data") and isinstance(request.data, dict):
            uid = request.data.get("user_id")
        if not uid:
            return None
        return self.cache_format % {
            "scope": self.scope,
            "ident": str(uid),
        }


class AnonIpRateThrottle(BaseInventoryThrottle):
    scope = "inventory_anon"

    def get_cache_key(self, request, view):
        uid = _get_authenticated_user_id(request)
        if not uid and hasattr(request, "data") and isinstance(request.data, dict):
            uid = request.data.get("user_id")
        if uid:
            return None  # Authenticated requests are throttled per user, not IP anon limit
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class SeatListRateThrottle(BaseInventoryThrottle):
    """
    Rate limits seat viewing requests to 10/min per anonymous session ID.
    If no session ID is present, falls back to client IP.
    Internal service and staff calls are exempt.
    """
    scope = "inventory_seat_list"
    fallback_scope = "inventory_anon"

    def get_cache_key(self, request, view):
        if request.method != "GET":
            return None

        # Check for persistent anonymous session ID
        session_id = _get_session_id(request)
        if session_id:
            ident = f"session_{session_id}"
        else:
            ident = f"ip_{self.get_ident(request)}"

        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }


class CheckReservationRateThrottle(BaseInventoryThrottle):
    scope = "inventory_check"
    fallback_scope = "inventory_anon"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class ReserveRateThrottle(UserActionRateThrottle):
    scope = "inventory_reserve"
    fallback_scope = "inventory_user"


class ReserveAnonRateThrottle(AnonIpRateThrottle):
    scope = "inventory_reserve_anon"
    fallback_scope = "inventory_anon"


class ReleaseRateThrottle(UserActionRateThrottle):
    scope = "inventory_release"
    fallback_scope = "inventory_user"


class ReleaseAnonRateThrottle(AnonIpRateThrottle):
    scope = "inventory_release_anon"
    fallback_scope = "inventory_anon"
