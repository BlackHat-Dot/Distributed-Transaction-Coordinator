import secrets

from django.conf import settings
from rest_framework.exceptions import Throttled
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import exception_handler

from .auth import _get_authenticated_user_id


# cache_format = 'throttle_%(scope)s_%(ident)s'

# why self.scope is included?

# without
# # BAD KEY DESIGN (No scope):
# "throttle_192.168.1.1"

# with
# "throttle_inventory_seat_list_192.168.1.1"   # Counter for viewing seats (limit: 300)
# "throttle_inventory_reserve_192.168.1.1"     # Counter for reserving seats (limit: 5)

def inventory_exception_handler(exc, context):
    response = exception_handler(exc, context) # ensures we don't accidentally swallow real bugs or silence internal 500 server crashes.
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

    # reached by __init__
    def get_rate(self):
        rf_settings = getattr(settings, "REST_FRAMEWORK", {}) or {}
        rates = rf_settings.get("DEFAULT_THROTTLE_RATES", {}) or {}
        if self.scope in rates:
            return rates[self.scope]
        if self.fallback_scope and self.fallback_scope in rates:
            return rates[self.fallback_scope]
        return super().get_rate()

    # reached by HTTP request
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
    scope = "inventory_seat_list"
    fallback_scope = "inventory_anon"

    # generates the unique tracking label stored in Redis
    def get_cache_key(self, request, view):
        # Public catalog endpoint: throttled per client IP with NAT-aware capacity
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request), # extracts client's IP address
        }
    
    # --- EXECUTION FLOW ---
    #
    # 1. An HTTP request arrives at GET /seats/.
    #
    # 2. Django executes BaseInventoryThrottle.allow_request(request, view):
    #    - Checks if the request header contains a valid X-Internal-Service-Key or X-Staff-Key.
    #    - If valid, returns True immediately (skips rate limiting).
    #
    # 3. If not exempt, it calls super().allow_request(request, view) on SimpleRateThrottle:
    #    - SimpleRateThrottle calls SeatListRateThrottle.get_cache_key() to get the Redis key string.
    #    - SimpleRateThrottle queries Redis for that key's timestamp history.
    #    - Compares request count against the rate limit (300/min).
    #    - Returns True (allow request) or False (block request with HTTP 429).


class CheckReservationRateThrottle(BaseInventoryThrottle):
    scope = "inventory_check"
    fallback_scope = "inventory_anon"

    def get_cache_key(self, request, view):
        # Transaction status query: throttled per client IP
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

