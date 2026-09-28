import logging
from django.db import DatabaseError, OperationalError
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger("payments")


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)

    if response is None:
        if isinstance(exc, (OperationalError, DatabaseError)):
            logger.error("Database error caught in exception handler: %s", exc, exc_info=True)
            return Response(
                {
                    "error": "Database service unavailable",
                    "code": "DATABASE_UNAVAILABLE",
                },
                status=503,
            )

    return response
