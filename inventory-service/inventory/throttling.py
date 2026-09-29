import secrets

from django.conf import settings
from rest_framework.exceptions import Throttled
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import exception_handler

from .auth import _get_authenticated_user_id


def inventory_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if isinstance(exc, Throttled) and response is not None:
        wait = getattr(exc, "wait", None)
        msg = f"Request was throttled. Expected available in {int(wait)} seconds." if wait else "Request was throttled."
        response.data = {
            "error": msg,
            "code": "THROTTLED",
            "available_in_seconds": int(wait) if wait else None,
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
        ident = uid or self.get_ident(request)
        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }


class AnonIpRateThrottle(BaseInventoryThrottle):
    scope = "inventory_anon"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class SeatListRateThrottle(AnonIpRateThrottle):
    scope = "inventory_seat_list"
    fallback_scope = "inventory_anon"


class ReserveRateThrottle(UserActionRateThrottle):
    scope = "inventory_reserve"
    fallback_scope = "inventory_user"


class ReleaseRateThrottle(UserActionRateThrottle):
    scope = "inventory_release"
    fallback_scope = "inventory_user"


class CheckReservationRateThrottle(AnonIpRateThrottle):
    scope = "inventory_check"
    fallback_scope = "inventory_anon"
