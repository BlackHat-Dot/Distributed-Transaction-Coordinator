import contextvars
import logging

_correlation_id_ctx = contextvars.ContextVar("correlation_id", default="AnonUser")


def get_correlation_id():
    return _correlation_id_ctx.get()


def set_correlation_id(transaction_id):
    return _correlation_id_ctx.set(str(transaction_id) if transaction_id else "AnonUser")


class TransactionIdFilter(logging.Filter):
    """Injects current transaction/worker context into log records."""

    def filter(self, record):
        tid = get_correlation_id()
        record.log_tag = "worker=bgworker" if tid == "bgworker" else f"transaction={tid}"
        return True


class CorrelationIdMiddleware:
    """
    Assigns transaction tracking to execution context.
    Unauthenticated users and non-transaction fetches default to 'AnonUser'.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tid = request.META.get("HTTP_X_CORRELATION_ID") or "AnonUser"
        token = set_correlation_id(tid)
        try:
            return self.get_response(request)
        finally:
            _correlation_id_ctx.reset(token)


TransactionIdMiddleware = CorrelationIdMiddleware
