import hmac
import os
from django.conf import settings
from rest_framework.exceptions import Throttled
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import exception_handler

from coordinator.auth_views import get_user_from_request


def _is_exempt(request):
    staff_key = os.getenv("STAFF_KEY")
    req_staff = request.headers.get("X-Staff-Key")

    if staff_key and req_staff and hmac.compare_digest(req_staff.strip(), staff_key.strip()):
        return True
    return False


def _resolve_ident(request, throttle_instance):
    user_info = get_user_from_request(request)
    if user_info and user_info.get("is_authenticated") and user_info.get("user_id"):
        return str(user_info["user_id"])
    if (getattr(settings, "IS_TESTING", False) or getattr(settings, "DEBUG", False)) and request.headers.get("X-User-ID"):
        return str(request.headers.get("X-User-ID"))
    return throttle_instance.get_ident(request)


class BookingRateThrottle(SimpleRateThrottle):
    """
    Unified booking rate limiter: 4 POST requests per minute.
    Applies across both holds and payments. Once exhausted on the 4th attempt,
    the user must wait 60 seconds before they can confirm or make another request.
    (Held seats are safely preserved since hold duration is 10 minutes).
    """
    scope = "coordinator_transactions"

    def allow_request(self, request, view):
        if _is_exempt(request):
            return True
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        if request.method != "POST":
            return None  # GETs (listing transactions) do not consume booking quota

        ident = _resolve_ident(request, self)
        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }


def coordinator_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if isinstance(exc, Throttled) and response is not None:
        wait = getattr(exc, "wait", None)
        wait_seconds = max(1, int(wait)) if wait is not None else 60
        response.data = {
            "error": f"Request limit exceeded. Try again in {wait_seconds} seconds.",
            "code": "THROTTLED",
            "wait": wait_seconds,
        }
    return response
