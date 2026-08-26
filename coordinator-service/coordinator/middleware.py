import contextvars
import json
import logging
import uuid

_correlation_id_ctx = contextvars.ContextVar("correlation_id", default="")


def get_correlation_id():
    return _correlation_id_ctx.get() or ""


def set_correlation_id(correlation_id):
    return _correlation_id_ctx.set(correlation_id)


class CorrelationIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        correlation_id = request.META.get("HTTP_X_CORRELATION_ID")

        if not correlation_id:
            correlation_id = request.GET.get("transaction_id")

        if not correlation_id and request.body:
            try:
                data = json.loads(request.body.decode("utf-8"))
                if isinstance(data, dict):
                    correlation_id = data.get("transaction_id")
            except Exception:
                pass

        if not correlation_id:
            correlation_id = str(uuid.uuid4())
        else:
            correlation_id = str(correlation_id)

        token = set_correlation_id(correlation_id)
        request.correlation_id = correlation_id

        try:
            response = self.get_response(request)
        finally:
            _correlation_id_ctx.reset(token)

        response["X-Correlation-ID"] = correlation_id
        return response


class CorrelationIdFilter(logging.Filter):
    def filter(self, record):
        cid = get_correlation_id()
        record.correlation_id = cid if cid else "-"
        return True
