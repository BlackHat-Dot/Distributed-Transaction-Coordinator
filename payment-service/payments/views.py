import uuid
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.db import IntegrityError, transaction

from .models import Payment
from .permissions import verify_internal_service_key, InternalServicePermission

_verify_internal_service_key = verify_internal_service_key


TESTING_FAILURE_AMOUNT = Decimal("9999")

TWO_PLACES = Decimal("0.01")


def _get_max_amount():
    field = Payment._meta.get_field("amount")
    max_int_digits = field.max_digits - field.decimal_places
    return Decimal("9" * max_int_digits + "." + "9" * field.decimal_places)


MAX_PAYMENT_AMOUNT = _get_max_amount()

NOT_CHARGED = "NOT_CHARGED"


@api_view(["GET"])
def health_check(request):
    return Response({"status": "healthy", "service": "payment"})


def _validate_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _require_transaction_id(raw_value):
    if not raw_value:
        return None, Response({"error": "transaction_id is required"}, status=400)

    transaction_uuid = _validate_uuid(raw_value)
    if not transaction_uuid:
        return None, Response({"error": "transaction_id must be a valid UUID"}, status=400)

    return transaction_uuid, None


def _validate_amount(raw_amount):
    if raw_amount is None:
        return None, Response({"error": "amount is required"}, status=400)

    try:
        amount = Decimal(str(raw_amount))
    except (InvalidOperation, TypeError):
        return None, Response({"error": "amount must be a valid number"}, status=400)

    if not amount.is_finite():
        return None, Response({"error": "amount must be a finite number"}, status=400)

    # F7: Reject inputs with more than 2 decimal places (do not silently round)
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        return None, Response({"error": "amount cannot have more than 2 decimal places"}, status=400)

    if amount <= 0:
        return None, Response({"error": "amount must be greater than zero"}, status=400)

    # F8: Upper bound check derived dynamically from Payment model field definition
    if amount > MAX_PAYMENT_AMOUNT:
        return None, Response(
            {"error": f"amount exceeds maximum allowable limit of {MAX_PAYMENT_AMOUNT}"},
            status=400,
        )

    return amount.quantize(TWO_PLACES), None


def _validate_user_info(data):
    user_id = None
    if "user_id" in data and data["user_id"] is not None:
        raw_uid = data["user_id"]
        uid_str = str(raw_uid).strip()
        if uid_str != "":
            max_uid_len = Payment._meta.get_field("user_id").max_length
            if len(uid_str) > max_uid_len:
                return None, None, Response(
                    {"error": f"user_id exceeds maximum length of {max_uid_len}"},
                    status=400,
                )
            user_id = uid_str

    user_email = None
    if "user_email" in data and data["user_email"] is not None:
        raw_email = data["user_email"]
        email_str = str(raw_email).strip()
        if email_str != "":
            max_email_len = Payment._meta.get_field("user_email").max_length
            if len(email_str) > max_email_len:
                return None, None, Response(
                    {"error": f"user_email exceeds maximum length of {max_email_len}"},
                    status=400,
                )
            try:
                validate_email(email_str)
            except ValidationError:
                return None, None, Response(
                    {"error": "user_email must be a valid email address"},
                    status=400,
                )
            user_email = email_str

    return user_id, user_email, None


def _payment_response(payment, status=None):
    return Response(
        {
            "transaction_id": str(payment.transaction_id),
            "user_id": payment.user_id,
            "user_email": payment.user_email,
            "amount": str(payment.amount),
            "status": payment.status,
        },
        status=status,
    )


@api_view(["POST"])
def charge_payment(request):
    auth_err = _verify_internal_service_key(request)
    if auth_err:
        return auth_err

    if not isinstance(request.data, Mapping):
        return Response({"error": "Request body must be a valid JSON object"}, status=400)

    transaction_uuid, error_response = _require_transaction_id(request.data.get("transaction_id"))
    if error_response:
        return error_response

    amount, error_response = _validate_amount(request.data.get("amount"))
    if error_response:
        return error_response

    user_id, user_email, error_response = _validate_user_info(request.data)
    if error_response:
        return error_response

    # Payment decline flag
    if amount == TESTING_FAILURE_AMOUNT:
        return Response(
            {
                "error": "Payment declined",
                "transaction_id": str(transaction_uuid),
                "user_id": user_id,
                "user_email": user_email,
                "status": NOT_CHARGED,
                "_testing": "Intentional failure for compensation testing",
            },
            status=422,
        )

    existing = Payment.objects.filter(transaction_id=transaction_uuid).first()
    if existing:
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
    auth_err = _verify_internal_service_key(request)
    if auth_err:
        return auth_err

    transaction_uuid, error_response = _require_transaction_id(request.query_params.get("transaction_id"))
    if error_response:
        return error_response

    payment = Payment.objects.filter(transaction_id=transaction_uuid).first()
    if payment:
        return _payment_response(payment, status=200)

    return Response({"transaction_id": str(transaction_uuid), "status": NOT_CHARGED})


@api_view(["POST"])
def refund_payment(request):
    auth_err = _verify_internal_service_key(request)
    if auth_err:
        return auth_err

    if not isinstance(request.data, Mapping):
        return Response({"error": "Request body must be a valid JSON object"}, status=400)

    transaction_uuid, error_response = _require_transaction_id(request.data.get("transaction_id"))
    if error_response:
        return error_response

    payment = Payment.objects.filter(transaction_id=transaction_uuid).first()

    if not payment:
        return Response(
            {
                "error": "Payment not found",
                "transaction_id": str(transaction_uuid),
            },
            status=404,
        )

    # Idempotent: multiple refunds for the same transaction return the existing refunded record
    if payment.status == Payment.Status.REFUNDED:
        return _payment_response(payment, status=200)

    # Reject refund for transactions that were not charged
    if payment.status != Payment.Status.CHARGED:
        return Response(
            {
                "error": f"Cannot refund transaction with status {payment.status}",
                "transaction_id": str(transaction_uuid),
                "status": payment.status,
            },
            status=400,
        )

    with transaction.atomic():
        # Acquire row-level lock to serialize concurrent refund attempts
        p = Payment.objects.select_for_update().get(id=payment.id)

        if p.status == Payment.Status.REFUNDED:
            return _payment_response(p, status=200)
        
        if p.status != Payment.Status.CHARGED:
            return Response(
                {
                    "error": f"Cannot refund transaction with status {p.status}",
                    "transaction_id": str(transaction_uuid),
                    "status": p.status,
                },
                status=400,
            )

        p.status = Payment.Status.REFUNDED
        p.save(update_fields=["status"])
        
        return _payment_response(p, status=200)