# verified

import logging
import math
from collections.abc import Mapping
from django.db import DatabaseError, OperationalError
from rest_framework.exceptions import Throttled
from rest_framework.response import Response
from rest_framework.views import exception_handler

from .middleware import get_correlation_id, set_correlation_id

logger = logging.getLogger("payments")


def _extract_transaction_id(request):
    """
    Safely extracts transaction or correlation ID from request headers,
    query parameters, or parsed body mapping without triggering stream parse errors.
    """
    if not request:
        return None

    try:
        header_cid = request.headers.get("X-Correlation-ID") or request.headers.get("X-Transaction-ID")
        if header_cid and str(header_cid).strip():
            return str(header_cid).strip()
    except Exception:
        pass

    try:
        qp = getattr(request, "query_params", None)
        if isinstance(qp, Mapping):
            tid = qp.get("transaction_id")
            if tid and str(tid).strip():
                return str(tid).strip()
    except Exception:
        pass

    try:
        if hasattr(request, "_full_data") and isinstance(request._full_data, Mapping):
            tid = request._full_data.get("transaction_id")
            if tid and str(tid).strip():
                return str(tid).strip()
    except Exception:
        pass

    return None


def custom_exception_handler(exc, context):
    """
    Custom exception handler for payments service:
    1. Binds transaction/correlation ID from request context to execution context.
    2. Normalizes DRF Throttled exceptions into standardized DTC envelope.
    3. Maps Django DatabaseError / OperationalError to 503 DATABASE_UNAVAILABLE.
    4. Normalizes DRF 'detail' fields into 'error' for contract consistency.
    """
    request = context.get("request") if isinstance(context, dict) else None
    if request:
        cid = _extract_transaction_id(request)
        if cid and get_correlation_id() == "AnonUser":
            set_correlation_id(cid)

    response = exception_handler(exc, context)

    # Standardize Throttled response to DTC error contract
    if isinstance(exc, Throttled) and response is not None:
        try:
            wait = max(1, math.ceil(float(exc.wait))) if exc.wait is not None else 60
        except (TypeError, ValueError, OverflowError):
            wait = 60

        response.data = {
            "error": f"Something went wrong. Please wait for {wait} seconds.",
            "code": "THROTTLED",
            "wait": wait,
            "available_in_seconds": wait,
        }
        return response

    # Normalize 'detail' -> 'error' if missing
    if response is not None and isinstance(response.data, dict):
        if "detail" in response.data and "error" not in response.data:
            response.data["error"] = str(response.data["detail"])

    # Map unhandled database connection & operational errors to 503
    if response is None:
        if isinstance(exc, (OperationalError, DatabaseError)):
            path = getattr(request, "path", "unknown") if request else "unknown"
            method = getattr(request, "method", "unknown") if request else "unknown"
            logger.error(
                "Database error caught in exception handler: %s [path=%s, method=%s, correlation_id=%s]",
                exc,
                path,
                method,
                get_correlation_id(),
                exc_info=True,
            )
            return Response(
                {
                    "error": "Database service unavailable",
                    "code": "DATABASE_UNAVAILABLE",
                },
                status=503,
            )

    return response