from django.conf import settings
from rest_framework.exceptions import Throttled
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import exception_handler

from .auth import _is_staff_request, _verify_internal_service_key, _get_authenticated_user_id


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


class UserActionRateThrottle(SimpleRateThrottle):
    scope = "inventory_user"

    def get_rate(self):
        rf_settings = getattr(settings, "REST_FRAMEWORK", {}) or {}
        rates = rf_settings.get("DEFAULT_THROTTLE_RATES", {}) or {}
        if self.scope in rates:
            return rates[self.scope]
        return super().get_rate()

    def allow_request(self, request, view):
        if _is_staff_request(request):
            return True
        if request.headers.get("X-Internal-Service-Key"):
            auth_err, is_internal = _verify_internal_service_key(request, allow_client=True)
            if is_internal:
                return True
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        uid = _get_authenticated_user_id(request)
        if not uid and hasattr(request, "data") and isinstance(request.data, dict):
            uid = request.data.get("user_id")
        ident = uid or self.get_ident(request)
        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }


class AnonIpRateThrottle(SimpleRateThrottle):
    scope = "inventory_anon"

    def get_rate(self):
        rf_settings = getattr(settings, "REST_FRAMEWORK", {}) or {}
        rates = rf_settings.get("DEFAULT_THROTTLE_RATES", {}) or {}
        if self.scope in rates:
            return rates[self.scope]
        return super().get_rate()

    def allow_request(self, request, view):
        if _is_staff_request(request):
            return True
        if request.headers.get("X-Internal-Service-Key"):
            auth_err, is_internal = _verify_internal_service_key(request, allow_client=True)
            if is_internal:
                return True
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }
