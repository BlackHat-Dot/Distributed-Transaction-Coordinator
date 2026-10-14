import contextvars
import logging
import uuid

_correlation_id_ctx = contextvars.ContextVar("correlation_id", default="")
# Module => contextvars(Context Varaibles) : Its job is to hold data that is private to the currently executing request/task, safe across both multi-threading and async code.
# Class => The Python class that represents an isolated storage container.
# Unlike a regular variable like x = "", an instance of ContextVar knows how to give different values to different threads.
# coorelation_id : This is the internal name / label for the variable.
# default="" : fallback value


# I know
def set_correlation_id(correlation_id):
    # Puts the ID into the current request's private locker
    return _correlation_id_ctx.set(correlation_id)

# I know
def get_correlation_id():
    # Opens the current request's locker and returns the ID
    return _correlation_id_ctx.get() or ""


# fucking golden rule: Because all requests share the same single class instance, NEVER store request data on self

# This class written inside MIDDLEWARE list in settings.py
# During server startup (WSGIHandler.load_middleware):
# Django creates the instance ONE TIME:
# middleware_instance = CorrelationIdMiddleware(get_response=next_middleware)

# When the request arrives Django does NOT call CorrelationIdMiddleware(...) again (so __init__ is never called again).
# Instead, Django takes the existing instance that is already sitting in memory and calls it like a function (middleware_instance()) -> triggers __call__
# I know
class CorrelationIdMiddleware:
    # For Django
    def __init__(self, get_response):
        # get_response is a callable (a function) representing the next layer in the chain(Middleware chain).
        # initialized in every middleware layer, every layer knows it's next layer, by get_response
        self.get_response = get_response

    # For request that arrives
    def __call__(self, request):
        correlation_id = str(uuid.uuid4())

        token = set_correlation_id(correlation_id)

        # attaches correlation_id to the request instance, so any view can access it
        request.correlation_id = correlation_id

        try:
            # "I have set up the correlation ID for this request; now pass the request deeper into the application so the view can run and return a response."
            response = self.get_response(request)
        finally:
            # wipes the correlation ID out of this thread's memory
            _correlation_id_ctx.reset(token)


        # Sets the correlation id onto the outgoing reponse header
        response["X-Correlation-ID"] = correlation_id

        # Send back to client
        return response

# I know
class CorrelationIdFilter(logging.Filter): # Inherits from logging.Filter
    # This class tells to python that whenver a log message is about to be printed, pass it through this class first.
    def filter(self, record):
        # Any time the app runs logger.info("..."), Python creates an internal Logrecord object called record
        # record object comes with standard built-in field and i add correlation_id as a extra field
        cid = get_correlation_id()

        # if correlation_id isn't exist then fall back to hyphen(-)
        record.correlation_id = cid if cid else "-"

        # In python's logging.Filter ,returning `True` means "allow this log record to be logged",
        # If returned False , python would drop and silence the log message entirely.
        return True
