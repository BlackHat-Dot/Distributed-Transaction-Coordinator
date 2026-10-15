import contextvars
import logging
import uuid

_correlation_id_ctx = contextvars.ContextVar("correlation_id", default="")


def set_correlation_id(correlation_id):
    """Set correlation ID for current execution context."""
    return _correlation_id_ctx.set(correlation_id)


def get_correlation_id():
    """Get correlation ID from current execution context."""
    return _correlation_id_ctx.get() or ""


class CorrelationIdMiddleware:
    """Assigns unique correlation ID to request and attaches it to response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        correlation_id = str(uuid.uuid4())
        token = set_correlation_id(correlation_id)
        request.correlation_id = correlation_id

        try:
            response = self.get_response(request)
        finally:
            _correlation_id_ctx.reset(token)

        response["X-Correlation-ID"] = correlation_id
        return response


class CorrelationIdFilter(logging.Filter):
    """Injects current correlation ID into log records."""

    def filter(self, record):
        cid = get_correlation_id()
        record.correlation_id = cid if cid else "-"
        return True
