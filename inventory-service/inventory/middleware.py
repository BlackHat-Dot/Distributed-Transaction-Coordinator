import contextvars
import logging

# Declares an asynchronous context variable (ContextVar) to propagate distributed correlation IDs across coroutines and threads.
_correlation_id_ctx = contextvars.ContextVar("correlation_id", default="AnonUser")


# Retrieves the active transaction ID or caller identifier from the current execution context for logging and downstream RPCs.
def get_correlation_id():
    return _correlation_id_ctx.get()


# Binds the target transaction or task UUID to the thread context, falling back to AnonUser when unprovided.
def set_correlation_id(transaction_id):
    return _correlation_id_ctx.set(str(transaction_id) if transaction_id else "AnonUser")


class TransactionIdFilter(logging.Filter):
    """Injects current transaction/worker context into log records."""

    def filter(self, record):
        tid = get_correlation_id()
        # Formats structured log tags to distinguish background Celery worker tasks from customer HTTP transactions in log aggregators.
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
        # Ingests the incoming X-Correlation-ID HTTP header propagated from the Coordinator service to link all inventory log events.
        tid = request.META.get("HTTP_X_CORRELATION_ID") or "AnonUser"
        token = set_correlation_id(tid)
        try:
            return self.get_response(request)
        finally:
            # Resets the context variable to its previous token state in a finally block to prevent context leakage across reused WSGI threads.
            _correlation_id_ctx.reset(token)


TransactionIdMiddleware = CorrelationIdMiddleware


class AnonymousSessionMiddleware:
    """
    Ensures legitimate anonymous clients always receive a cryptographically signed session token
    in the response headers and cookies, enabling session-based throttling without risk of forgery.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # If client provided a valid session ID, ensure it's echoed in the response
        raw_token = None
        if hasattr(request, "headers") and request.headers:
            raw_token = request.headers.get("X-Session-ID")
        elif hasattr(request, "META") and request.META:
            raw_token = request.META.get("HTTP_X_SESSION_ID")

        if raw_token and isinstance(raw_token, str) and raw_token.strip():
            from django.core.signing import TimestampSigner
            from .throttling import _SESSION_SIGNER_SALT
            signer = TimestampSigner(salt=_SESSION_SIGNER_SALT)
            try:
                signer.unsign(raw_token.strip(), max_age=86400 * 30)
                response["X-Session-ID"] = raw_token.strip()
                return response
            except Exception:
                pass

        # For requests without a valid session header, attach a signed session token
        if "X-Session-ID" not in response:
            from .throttling import issue_signed_session_id
            new_sid = issue_signed_session_id()
            response["X-Session-ID"] = new_sid
            response.set_cookie("dtc_anon_sid", new_sid, max_age=86400 * 30, httponly=True, samesite="Lax")

        return response

