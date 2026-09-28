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
            "amount": str(payment.amount) if payment.amount is not None else None,
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

    # Payment decline flag (F2: gated behind ENABLE_TEST_PAYMENT_FAILURE setting)
    enable_test_failure = getattr(settings, "ENABLE_TEST_PAYMENT_FAILURE", False) or getattr(settings, "IS_TESTING", False)
    if enable_test_failure and amount == TESTING_FAILURE_AMOUNT:
        response_data = {
            "error": "Payment declined",
            "transaction_id": str(transaction_uuid),
            "user_id": user_id,
            "user_email": user_email,
            "status": NOT_CHARGED,
        }
        if getattr(settings, "IS_TESTING", False) or getattr(settings, "DEBUG", False):
            response_data["_testing"] = "Intentional failure for compensation testing"
        return Response(response_data, status=422)

    existing = Payment.objects.filter(transaction_id=transaction_uuid).first()
    if existing:
        return _handle_existing_charge(existing, amount, user_id, transaction_uuid)

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
        # F10: Catch DoesNotExist in case integrity error was triggered by a non-PK constraint
        try:
            existing = Payment.objects.get(transaction_id=transaction_uuid)
        except Payment.DoesNotExist:
            return Response(
                {"error": "Database integrity constraint violation"},
                status=500,
            )
        return _handle_existing_charge(existing, amount, user_id, transaction_uuid)

    return _payment_response(payment, status=201)


def _handle_existing_charge(existing, amount, user_id, transaction_uuid):
    # F4: Attempt to charge a VOIDED transaction => 409
    if existing.status == Payment.Status.VOIDED:
        return Response(
            {
                "error": "Transaction has been voided",
                "code": "TRANSACTION_VOIDED",
                "status": NOT_CHARGED,
                "transaction_id": str(transaction_uuid),
            },
            status=409,
        )

    # F14: Charge replay on a REFUNDED payment => 409
    if existing.status == Payment.Status.REFUNDED:
        return Response(
            {
                "error": "Cannot charge already refunded transaction",
                "transaction_id": str(transaction_uuid),
                "status": Payment.Status.REFUNDED,
            },
            status=409,
        )

    # F3: On replay, compare normalized amount and user_id; mismatch => 409
    # A3 contract: Must NOT contain top-level status or payment dict
    if existing.amount != amount or (existing.user_id or None) != (user_id or None):
        return Response(
            {
                "error": "Idempotency key re-used with conflicting payload",
                "code": "IDEMPOTENCY_PAYLOAD_MISMATCH",
                "transaction_id": str(transaction_uuid),
                "existing_status": existing.status,
            },
            status=409,
        )

    return _payment_response(existing, status=200)


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
        if payment.status == Payment.Status.VOIDED:
            return Response(
                {
                    "transaction_id": str(transaction_uuid),
                    "status": NOT_CHARGED,
                    "voided": True,
                },
                status=200,
            )
        return _payment_response(payment, status=200)

    return Response({"transaction_id": str(transaction_uuid), "status": NOT_CHARGED})


@api_view(["POST"])
def void_payment(request):
    auth_err = _verify_internal_service_key(request)
    if auth_err:
        return auth_err

    if not isinstance(request.data, Mapping):
        return Response({"error": "Request body must be a valid JSON object"}, status=400)

    transaction_uuid, error_response = _require_transaction_id(request.data.get("transaction_id"))
    if error_response:
        return error_response

    with transaction.atomic():
        p = Payment.objects.select_for_update().filter(transaction_id=transaction_uuid).first()
        if p:
            if p.status == Payment.Status.VOIDED:
                return Response(
                    {
                        "transaction_id": str(transaction_uuid),
                        "status": NOT_CHARGED,
                        "voided": True,
                    },
                    status=200,
                )
            return _payment_response(p, status=200)

        try:
            with transaction.atomic():
                p = Payment.objects.create(
                    transaction_id=transaction_uuid,
                    amount=None,
                    status=Payment.Status.VOIDED,
                )
        except IntegrityError:
            p = Payment.objects.select_for_update().filter(transaction_id=transaction_uuid).first()
            if not p:
                return Response({"error": "Database integrity constraint violation"}, status=500)
            if p.status == Payment.Status.VOIDED:
                return Response(
                    {
                        "transaction_id": str(transaction_uuid),
                        "status": NOT_CHARGED,
                        "voided": True,
                    },
                    status=200,
                )
            return _payment_response(p, status=200)

    return Response(
        {
            "transaction_id": str(transaction_uuid),
            "status": NOT_CHARGED,
            "voided": True,
        },
        status=200,
    )


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

    # F20: Single lookup under select_for_update inside one atomic block
    with transaction.atomic():
        p = Payment.objects.select_for_update().filter(transaction_id=transaction_uuid).first()

        if not p:
            return Response(
                {
                    "error": "Payment not found",
                    "transaction_id": str(transaction_uuid),
                },
                status=404,
            )

        # Idempotent: multiple refunds for the same transaction return the existing refunded record
        if p.status == Payment.Status.REFUNDED:
            return _payment_response(p, status=200)

        # Reject refund for transactions that were not charged
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