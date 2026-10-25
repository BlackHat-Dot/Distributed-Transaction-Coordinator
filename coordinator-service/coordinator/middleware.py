import contextvars
import logging

_transaction_id_ctx = contextvars.ContextVar("transaction_id", default="AnonUser")


def set_transaction_id(transaction_id):
    """Set transaction ID for current execution context."""
    return _transaction_id_ctx.set(str(transaction_id) if transaction_id else "AnonUser")


def get_transaction_id():
    """Get transaction ID from current execution context."""
    return _transaction_id_ctx.get()


class TransactionIdFilter(logging.Filter):
    """Injects current transaction/worker context into log records."""

    def filter(self, record):
        tid = get_transaction_id()
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
        token = set_transaction_id("AnonUser")
        try:
            return self.get_response(request)
        finally:
            _transaction_id_ctx.reset(token)


TransactionIdMiddleware = CorrelationIdMiddleware
