import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.db import IntegrityError, transaction

from .models import Payment

TESTING_FAILURE_AMOUNT = Decimal("9999")

TWO_PLACES = Decimal("0.01")

NOT_CHARGED = "NOT_CHARGED"


@api_view(["GET"])
def health_check(request):
    return Response({"status": "healthy", "service": "payment"})


def _validate_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


def _require_transaction_id(raw_value):
    if not raw_value:
        return None, Response({"error": "transaction_id is required"}, status=400)

    transaction_uuid = _validate_uuid(raw_value)
    if transaction_uuid is None:
        return None, Response({"error": "transaction_id must be a valid UUID"}, status=400)

    return transaction_uuid, None


def _validate_amount(raw_amount):
    if raw_amount is None:
        return None, Response({"error": "amount is required"}, status=400)

    try:
        amount = Decimal(str(raw_amount)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError):
        return None, Response({"error": "amount must be a valid number"}, status=400)

    if amount <= 0:
        return None, Response({"error": "amount must be greater than zero"}, status=400)

    return amount, None


def _payment_response(payment, status=None):
    return Response(
        {
            "transaction_id": str(payment.transaction_id),
            "user_id": payment.user_id,
            "user_email": payment.user_email,
            "held_by_user_id": payment.user_id,
            "held_by_user_email": payment.user_email,
            "amount": str(payment.amount),
            "status": payment.status,
        },
        status=status,
    )


@api_view(["POST"])
def charge_payment(request):
    transaction_uuid, error_response = _require_transaction_id(request.data.get("transaction_id"))
    if error_response:
        return error_response

    amount, error_response = _validate_amount(request.data.get("amount"))
    if error_response:
        return error_response

    user_id = (
        request.data.get("user_id")
        or request.data.get("held_by_user_id")
        or request.data.get("actor_id")
        or request.headers.get("X-User-ID")
        or request.headers.get("X-Actor-ID")
        or "usr_anonymous"
    )
    user_email = (
        request.data.get("user_email")
        or request.data.get("held_by_user_email")
        or request.data.get("email")
        or request.headers.get("X-User-Email")
        or "anonymous@dtc.io"
    )

    if amount == TESTING_FAILURE_AMOUNT:
        return Response(
            {
                "error": "Payment declined",
                "transaction_id": str(transaction_uuid),
                "user_id": user_id,
                "user_email": user_email,
                "held_by_user_id": user_id,
                "held_by_user_email": user_email,
                "status": NOT_CHARGED,
                "_testing": "Intentional failure for compensation testing",
            },
            status=422,
        )

    existing = Payment.objects.filter(transaction_id=transaction_uuid).first()
    if existing is not None:
        return _payment_response(existing)

    try:
        with transaction.atomic():
            payment = Payment.objects.create(
                transaction_id=transaction_uuid,
                user_id=user_id,
                user_email=user_email,
                amount=amount,
                status=Payment.Status.CHARGED,
            )
    except IntegrityError:
        existing = Payment.objects.get(transaction_id=transaction_uuid)
        return _payment_response(existing)

    return _payment_response(payment, status=201)


@api_view(["GET"])
def payment_check(request):
    transaction_uuid, error_response = _require_transaction_id(request.query_params.get("transaction_id"))
    if error_response:
        return error_response

    payment = Payment.objects.filter(transaction_id=transaction_uuid).first()
    if payment is not None:
        return Response({
            "transaction_id": str(payment.transaction_id),
            "user_id": payment.user_id,
            "user_email": payment.user_email,
            "held_by_user_id": payment.user_id,
            "held_by_user_email": payment.user_email,
            "status": payment.status
        })

    return Response({"transaction_id": str(transaction_uuid), "status": NOT_CHARGED})