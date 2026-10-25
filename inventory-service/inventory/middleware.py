import contextvars
import logging
import uuid

_transaction_id_ctx = contextvars.ContextVar("transaction_id", default="AnonFetch")


def get_transaction_id():
    return _transaction_id_ctx.get() or "AnonFetch"


def set_transaction_id(transaction_id):
    return _transaction_id_ctx.set(str(transaction_id) if transaction_id else "AnonFetch")


# Aliases for backward compatibility
get_correlation_id = get_transaction_id
set_correlation_id = set_transaction_id


class TransactionIdFilter(logging.Filter):
    """Injects current transaction/worker context into log records."""

    def filter(self, record):
        tid = get_transaction_id()
        record.transaction_id = tid if tid else "AnonFetch"
        record.correlation_id = record.transaction_id
        if tid == "bgworker":
            record.log_tag = "worker=bgworker"
        else:
            record.log_tag = f"transaction={record.transaction_id}"
        return True


# Alias for backward compatibility with existing tests
CorrelationIdFilter = TransactionIdFilter


class CorrelationIdMiddleware:
    """
    Assigns transaction tracking to request and attaches headers to response.
    Unauthenticated users and non-transaction fetches default to 'AnonFetch'.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tid = request.META.get("HTTP_X_TRANSACTION_ID") or request.META.get("HTTP_X_CORRELATION_ID") or "AnonFetch"

        token = set_transaction_id(tid)
        request.transaction_id = tid
        request.correlation_id = tid

        try:
            response = self.get_response(request)
        finally:
            _transaction_id_ctx.reset(token)

        current_tid = get_transaction_id()
        response["X-Transaction-ID"] = current_tid
        response["X-Correlation-ID"] = current_tid
        return response


TransactionIdMiddleware = CorrelationIdMiddleware
