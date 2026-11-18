# verified

# Imports Python logging module to capture diagnostic telemetry and rate limit warnings.
import logging
# Imports standard math module for ceiling calculations when formatting retry-after delay durations.
import math

# Imports DRF exception classes representing authentication failures, permission denials, and rate throttled states.
from rest_framework.exceptions import NotAuthenticated, PermissionDenied, Throttled
# Imports DRF base permission class to implement custom authentication guards on booking endpoints.
from rest_framework.permissions import BasePermission
# Imports DRF's base throttle class which calculates window durations and formats Redis cache keys for rate limiting.
from rest_framework.throttling import SimpleRateThrottle
# Imports DRF standard exception handler to provide baseline error response formatting.
from rest_framework.views import exception_handler

# Imports authentication identity and client IP extraction utilities from auth views.
from coordinator.auth_views import _get_caller, _get_client_ip

# Initializes module-scoped logger tagged with coordinator throttling namespace for telemetry.
logger = logging.getLogger(__name__)

# Standardized client-facing message returned when unauthenticated callers attempt ticket bookings.
AUTH_MSG = "Authentication required. Please sign in to book seats."


# Hardened rate throttle enforcing client identity using trusted IP extraction and atomic Redis counter increments.
class SafeRateThrottle(SimpleRateThrottle):
    """
    Base throttle that safely identifies clients using verified IP
    instead of spoofable raw X-Forwarded-For headers.
    """
    # Securely resolves the client's network address using trusted socket remote_addr rather than spoofable client headers.
    def get_ident(self, request):
        # Attempts to resolve verified client IP using trusted reverse proxy header extraction.
        try:
            ip = _get_client_ip(request)
            if ip and ip != "unknown":
                return ip
        except Exception as exc:
            logger.warning("Failed to resolve client IP in SafeRateThrottle: %s", exc)

        # Fallback to direct REMOTE_ADDR socket IP if application-level client IP detection returns unknown or raises exceptions.
        # Extracts socket-level REMOTE_ADDR from WSGI/ASGI request META dictionary as fallback.
        meta = getattr(request, "META", None)
        # Validates socket metadata dictionary and extracts non-empty REMOTE_ADDR string if present.
        if meta and hasattr(meta, "get"):
            remote_addr = meta.get("REMOTE_ADDR")
            if isinstance(remote_addr, str) and remote_addr.strip():
                return remote_addr.strip()
        # Returns unknown sentinel if neither trusted reverse proxy headers nor socket address could be identified.
        return "unknown"

    # Evaluates request allowance using an atomic fixed-window rate limiting algorithm backed by Redis cache operations.
    def allow_request(self, request, view):
        # Bypass check: allows all traffic immediately if rate limit string is not configured on throttle instance.
        if self.rate is None:
            return True

        # Resolves unique cache key for current request; returns True to allow traffic if key is None (e.g. staff or unthrottled methods).
        self.key = self.get_cache_key(request, view)
        if self.key is None:
            return True

        # Atomic fixed-window rate limiting using cache.add + cache.incr
        # Atomic key initialization: sets counter to 0 with window TTL only if key does not already exist (SET NX equivalent).
        self.cache.add(self.key, 0, timeout=self.duration)
        try:
            # Atomically increments the request counter; raises ValueError if key expired between add and incr calls.
            count = self.cache.incr(self.key)
        # Handles key expiration between add and incr calls by re-initializing the counter to 1 with full TTL.
        except ValueError:
            self.cache.set(self.key, 1, timeout=self.duration)
            count = 1

        # Guarantee TTL if key was recreated by incr without expiry
        # TOCTOU safety guarantee: ensures key always has a valid TTL if recreated by incr without expiry during high concurrency.
        if count == 1:
            self.cache.touch(self.key, self.duration)

        # Compares active counter against allowed limit, returning False to trigger 429 Throttled response if exceeded.
        if count > self.num_requests:
            return False
        return True

    # Computes remaining lockout seconds by querying the live Redis key TTL, falling back to full window duration on error.
    def wait(self):
        """
        Returns the exact remaining TTL (seconds) for the current window on Redis,
        falling back to the full window duration if TTL is unavailable.
        """
        # Inspects cache key to query remaining TTL directly from Redis backend.
        if self.key:
            # 1. Direct ttl() support (e.g. django-redis)
            # Checks for direct django-redis ttl() support to retrieve exact remaining seconds in the rate window.
            if hasattr(self.cache, "ttl"):
                try:
                    ttl = self.cache.ttl(self.key)
                    if ttl is not None and ttl > 0:
                        return ttl
                except Exception:
                    pass

            # 2. Django built-in RedisCache (inspects internal _cache client; falls back to duration on changes)
            # Direct RedisCache driver inspection: queries raw Redis client for exact key TTL in seconds.
            if hasattr(self.cache, "_cache") and hasattr(self.cache._cache, "get_client"):
                try:
                    client = self.cache._cache.get_client()
                    full_key = self.cache.make_and_validate_key(self.key)
                    ttl = client.ttl(full_key)
                    if ttl is not None and ttl > 0:
                        return ttl
                except Exception:
                    pass

        return self.duration


# Permission class enforcing authentication while proactively evaluating rate limits on unauthenticated callers to stop DoS attacks.
class IsBookingAuthenticated(BasePermission):
    def has_permission(self, request, view):
        caller = _get_caller(request)
        if caller and caller.is_authenticated:
            return True

        # Security Guard: enforces view throttle check before raising 401 to ensure unauthenticated spam still triggers 429 rate limiting.
        if hasattr(view, "check_throttles"):
            view.check_throttles(request)
        raise NotAuthenticated(detail=AUTH_MSG)


# Enforces transaction creation rate limit (4/min) per authenticated user to prevent ticket hoarding and booking bot spam.
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

        # Resolves requester identity: exempts authorized staff operators while rate limiting standard users and unauthenticated IPs.
        caller = _get_caller(request)
        if caller and caller.is_staff:  # staff skip throttling
            return None

        # Scopes throttle key to authenticated user ID (user_<id>) or client IP (ip_<ip>) to prevent key collisions between callers.
        if caller and caller.is_authenticated and caller.user_id:
            ident = f"user_{caller.user_id}"
        else:
            ident = f"ip_{self.get_ident(request)}"

        return self.cache_format % {"scope": self.scope, "ident": ident}


# Enforces transaction fetch rate limit (60/min) per user/IP to prevent frontend polling loops from overwhelming coordinator database.
class TransactionReadRateThrottle(SafeRateThrottle):
    """
    Rate limits GET (fetch) requests per authenticated user to prevent DB DoS/polling spam.
    Falls back to client IP for unauthenticated callers to prevent cache collision DoS.
    The rate comes from DEFAULT_THROTTLE_RATES["coordinator_transaction_reads"] (60/min).
    Attach to transaction views together with IsBookingAuthenticated.
    """
    scope = "coordinator_transaction_reads"

    def get_cache_key(self, request, view):
        if request.method != "GET":
            return None

        # Resolves requester identity: exempts authorized staff operators while rate limiting standard users and unauthenticated IPs.
        caller = _get_caller(request)
        if caller and caller.is_staff:  # staff skip throttling
            return None

        # Scopes throttle key to authenticated user ID (user_<id>) or client IP (ip_<ip>) to prevent key collisions between callers.
        if caller and caller.is_authenticated and caller.user_id:
            ident = f"user_{caller.user_id}"
        else:
            ident = f"ip_{self.get_ident(request)}"

        return self.cache_format % {"scope": self.scope, "ident": ident}


# Restricts recovery health polling to 1/min per verified IP to protect background sweeper metrics from denial of service.
class RecoveryStatusRateThrottle(SafeRateThrottle):
    """
    Rate limits recovery status requests to 1/min per verified client IP to prevent polling spam.
    """
    scope = "coordinator_recovery_status"
    rate = "1/min"

    # Builds Redis cache key using the coordinator_recovery_status scope and verified client IP address.
    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


# Custom DRF exception handler providing human-friendly JSON error payloads with precise retry-after headers on 429 and 401 faults.
def coordinator_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None

    # Intercepts Throttled exceptions to extract exact lockout delay and build specialized polling vs write rate limit messages.
    if isinstance(exc, Throttled):
        try:
            wait = max(1, math.ceil(float(exc.wait))) if exc.wait is not None else 60
        except (TypeError, ValueError, OverflowError):
            wait = 60

        # Differentiates GET requests (checking status too often) from POST requests (request limit exceeded) for user-friendly UI copy.
        req = context.get("request") if isinstance(context, dict) else None
        if req and getattr(req, "method", None) == "GET":
            error_msg = f"You are checking status too often. Please wait {wait} seconds before trying again."
        else:
            error_msg = f"Request limit exceeded. Try again in {wait} seconds."
        # Attaches standardized error response dictionary containing wait duration in seconds for frontend countdown timers.
        response.data = {
            "error": error_msg,
            "code": "THROTTLED",
            "wait": wait,
            "available_in_seconds": wait,
        }
    # Normalizes authentication and permission rejection errors into uniform 401 AUTHENTICATION_REQUIRED JSON responses.
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
    # Returns the formatted DRF Response object containing status code and structured JSON error payload.
    return response