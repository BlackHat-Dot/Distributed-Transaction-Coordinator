# Imports Python's built-in contextvars module to store execution-context data safely across async tasks and multithreaded requests without cross-talk.
import contextvars

# Imports Python's standard logging library to customize log formatting and dynamically inject distributed tracking tags.
import logging

# ContextVar -> A thread-safe, coroutine-aware variable that holds the current transaction or caller ID scoped strictly to the active request flow.
_correlation_id_ctx = contextvars.ContextVar("correlation_id", default="AnonUser")


# Helper function to explicitly bind a transaction ID or worker identifier to the current thread's execution context.
def set_correlation_id(transaction_id):
    """Set transaction ID for current execution context."""
    # Sets the correlation ID in the context var, falling back to 'AnonUser' if None/empty, and returns a reset token for cleanup.
    return _correlation_id_ctx.set(str(transaction_id) if transaction_id else "AnonUser")


# Helper function to retrieve the active transaction ID or caller label from anywhere in the call stack without passing request objects.
def get_correlation_id():
    """Get transaction ID from current execution context."""
    return _correlation_id_ctx.get()


# Custom logging filter that enriches every emitted log record with the current transaction context for log aggregation and debugging.
class TransactionIdFilter(logging.Filter):
    """Injects current transaction/worker context into log records."""

    # Intercepts each log record before output to extract the correlation ID and attach a formatted log_tag attribute.
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
