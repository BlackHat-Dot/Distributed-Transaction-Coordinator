import contextvars
import logging

_correlation_id_ctx = contextvars.ContextVar("correlation_id", default="AnonUser")


def set_correlation_id(transaction_id):
    """Set transaction ID for current execution context."""
    return _correlation_id_ctx.set(str(transaction_id) if transaction_id else "AnonUser")


def get_correlation_id():
    """Get transaction ID from current execution context."""
    return _correlation_id_ctx.get()


class TransactionIdFilter(logging.Filter):
    """Injects current transaction/worker context into log records."""

    def filter(self, record):
        tid = get_correlation_id()

        if tid == "AnonUser" and hasattr(record, "request"):
            tid = getattr(record.request, "correlation_id", "AnonUser")

        record.log_tag = "worker=bgworker" if tid == "bgworker" else f"transaction={tid}"
        return True


class CorrelationIdMiddleware:
    """
    Assigns transaction tracking to execution context.
    Defaults to 'AnonUser' until overridden by an authenticated transaction.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in ("GET") :
            initial_id = "DataFetch"
        else:
            initial_id = "AnonUser"

        request.correlation_id = initial_id

        token = set_correlation_id(initial_id)
        try:
            return self.get_response(request)
        finally:
            _correlation_id_ctx.reset(token)


TransactionIdMiddleware = CorrelationIdMiddleware
