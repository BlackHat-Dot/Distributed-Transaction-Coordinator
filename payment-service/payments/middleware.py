# verified

import contextvars
import logging
import re
import uuid

_correlation_id_ctx = contextvars.ContextVar("correlation_id", default="AnonUser")

# Restrict to safe characters and reasonable length to prevent HTTP header injection (CRLF) and log poisoning
_SAFE_CORRELATION_ID_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")


def get_correlation_id():
    """Get correlation ID from the current execution context."""
    return _correlation_id_ctx.get()


def set_correlation_id(transaction_id):
    """Set correlation ID for current execution context."""
    cid_str = str(transaction_id).strip() if transaction_id is not None else ""
    return _correlation_id_ctx.set(cid_str if cid_str else "AnonUser")


class TransactionIdFilter(logging.Filter):
    """Injects current transaction/worker context into log records."""

    def filter(self, record):
        tid = get_correlation_id()
        if not tid or tid == "AnonUser":
            if hasattr(record, "request"):
                tid = getattr(record.request, "correlation_id", "AnonUser")
        record.log_tag = "worker=bgworker" if tid == "bgworker" else f"transaction={tid}"
        return True


class CorrelationIdMiddleware:
    """
    Assigns transaction and correlation tracking to request execution context:
    1. Extracts X-Correlation-ID or X-Transaction-ID header if valid.
    2. Generates a new UUIDv4 if missing, empty, or containing unsafe characters.
    3. Binds the correlation ID to contextvars for thread/async-safe logging.
    4. Attaches correlation ID to request and propagates it on outgoing responses.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        raw_cid = request.META.get("HTTP_X_CORRELATION_ID") or request.META.get("HTTP_X_TRANSACTION_ID")

        if raw_cid and isinstance(raw_cid, str):
            cleaned = raw_cid.strip()
            if _SAFE_CORRELATION_ID_RE.match(cleaned):
                cid = cleaned
            else:
                cid = str(uuid.uuid4())
        else:
            cid = str(uuid.uuid4())

        request.correlation_id = cid
        token = set_correlation_id(cid)

        try:
            response = self.get_response(request)
            if response is not None and hasattr(response, "__setitem__"):
                response["X-Correlation-ID"] = cid
            return response
        finally:
            _correlation_id_ctx.reset(token)


TransactionIdMiddleware = CorrelationIdMiddleware