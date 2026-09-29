import contextvars
import logging
import uuid

_correlation_id_ctx = contextvars.ContextVar("correlation_id", default="")
# Module => contextvars(Context Varaibles) : Its job is to hold data that is private to the currently executing request/task, safe across both multi-threading and async code.
# Class => The Python class that represents an isolated storage container.
# Unlike a regular variable like x = "", an instance of ContextVar knows how to give different values to different threads.
# coorelation_id : This is the internal name / label for the variable.
# default="" : fallback value


def set_correlation_id(correlation_id):
    # Puts the ID into the current request's private locker
    return _correlation_id_ctx.set(correlation_id)

def get_correlation_id():
    # Opens the current request's locker and returns the ID
    return _correlation_id_ctx.get() or ""


class CorrelationIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        correlation_id = (
            request.headers.get("X-Correlation-ID")
            or request.META.get("HTTP_X_CORRELATION_ID")
            or str(uuid.uuid4())
        )

        token = set_correlation_id(correlation_id)

        # attaches correlation_id to the request instance, so any view can access it
        request.correlation_id = correlation_id

        try:
            # this hands the request down into Django: Next Middelware -> urls.py -> views.py
            response = self.get_response(request)
        finally:
            # wipes the correlation ID out of this thread's memory
            _correlation_id_ctx.reset(token)

        
        # Sets the correlation id onto the outgoinf reponse header
        response["X-Correlation-ID"] = correlation_id

        # Send back to client
        return response


class CorrelationIdFilter(logging.Filter): # Inherits from logging.Filter
    # This class tells to python that whenver a log message is about to be printed, pass it through this class first.
    def filter(self, record):
        # Any time the app runs logger.info("..."), Python creates an internal Logrecord object called record
        # record object comes with standard built-in field and i add correlation_id as a extra field
        cid = get_correlation_id()

        # if correlation_id isn't exist then fall back to hyphen(-)
        record.correlation_id = cid if cid else "-"

        return True
