import logging
import uuid
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Payment
from .permissions import verify_internal_service_key

logger = logging.getLogger("payments")

TESTING_FAILURE_AMOUNT = Decimal("9999")
TWO_PLACES = Decimal("0.01")
INVALID_BODY_MESSAGE = "Request body must be a valid JSON object"


def _mask_email(email):
    """Mask the local part of an email for logs, e.g. a***e@example.com."""
    if not email or "@" not in email:
        return None
    user, domain = email.split("@", 1)
    if len(user) <= 1:
        return f"{user}***@{domain}"
    return f"{user[0]}***{user[-1]}@{domain}"


def _get_max_amount():
    """Largest amount the Payment.amount column can hold."""
    field = Payment._meta.get_field("amount")
    integer_digits = field.max_digits - field.decimal_places
    return Decimal("9" * integer_digits + "." + "9" * field.decimal_places)


MAX_PAYMENT_AMOUNT = _get_max_amount()


@api_view(["GET"])
def health_check(request):
    return Response({"status": "healthy", "service": "payment"})


def _bad_request(message):
    return Response({"error": message}, status=400)


def _clean_text(value):
    """Stringify and strip; None becomes an empty string."""
    return "" if value is None else str(value).strip()


def _validate_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _require_transaction_id(raw_value):
    if not raw_value:
        return None, _bad_request("transaction_id is required")
    transaction_uuid = _validate_uuid(raw_value)
    if transaction_uuid is None:
        return None, _bad_request("transaction_id must be a valid UUID")
    return transaction_uuid, None


def _validate_amount(raw_amount):
    if raw_amount is None:
        return None, _bad_request("amount is required")

    try:
        amount = Decimal(str(raw_amount))
    except (InvalidOperation, TypeError):
        return None, _bad_request("amount must be a valid number")

    if not amount.is_finite():
        return None, _bad_request("amount must be a finite number")

    # Reject extra decimal places instead of rounding them away
    if amount.as_tuple().exponent < -2:
        return None, _bad_request("amount cannot have more than 2 decimal places")

    if amount <= 0:
        return None, _bad_request("amount must be greater than zero")

    if amount > MAX_PAYMENT_AMOUNT:
        return None, _bad_request(f"amount exceeds maximum allowable limit of {MAX_PAYMENT_AMOUNT}")

    return amount.quantize(TWO_PLACES), None


def _validate_user_info(data):
    """Return (user_id, user_email, error_response). Both fields are optional."""
    user_id = _clean_text(data.get("user_id")) or None
    max_uid = Payment._meta.get_field("user_id").max_length
    if user_id and len(user_id) > max_uid:
        return None, None, Response(
            {"error": f"user_id exceeds maximum length of {max_uid}"}, status=400
        )

    user_email = _clean_text(data.get("user_email")) or None
    if user_email:
        max_email = Payment._meta.get_field("user_email").max_length
        if len(user_email) > max_email:
            return None, None, Response(
                {"error": f"user_email exceeds maximum length of {max_email}"}, status=400
            )
        try:
            validate_email(user_email)
        except ValidationError:
            return None, None, Response(
                {"error": "user_email must be a valid email address"}, status=400
            )

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


def _voided_response(transaction_uuid):
    """Reply for a voided or tombstoned transaction."""
    return Response(
        {
            "transaction_id": str(transaction_uuid),
            "status": Payment.Status.NOT_CHARGED,
            "voided": True,
        },
        status=200,
    )


def _lock_payment(transaction_uuid):
    """Fetch the payment row under a row lock (or None)."""
    return Payment.objects.select_for_update().filter(transaction_id=transaction_uuid).first()


@api_view(["POST"])
def charge_payment(request):
    auth_err = verify_internal_service_key(request)
    if auth_err:
        return auth_err

    if not isinstance(request.data, Mapping):
        return _bad_request(INVALID_BODY_MESSAGE)

    transaction_uuid, error = _require_transaction_id(request.data.get("transaction_id"))
    if error:
        return error

    amount, error = _validate_amount(request.data.get("amount"))
    if error:
        return error

    user_id, user_email, error = _validate_user_info(request.data)
    if error:
        return error

    # Simulated decline, only when the test-failure flag or testing is on
    is_testing = getattr(settings, "IS_TESTING", False)
    decline_enabled = getattr(settings, "ENABLE_TEST_PAYMENT_FAILURE", False) or is_testing
    if decline_enabled and amount == TESTING_FAILURE_AMOUNT:
        logger.info(
            "Payment simulated decline: transaction_id=%s amount=%s user_id=%s email=%s",
            transaction_uuid,
            amount,
            user_id,
            _mask_email(user_email),
        )
        response_data = {
            "error": "Payment declined",
            "transaction_id": str(transaction_uuid),
            "user_id": user_id,
            "user_email": user_email,
            "status": Payment.Status.NOT_CHARGED,
        }
        if is_testing or getattr(settings, "DEBUG", False):
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
        # Lost a race or hit a non-PK constraint: treat as a replay of the existing row
        try:
            existing = Payment.objects.get(transaction_id=transaction_uuid)
        except Payment.DoesNotExist:
            return Response({"error": "Database integrity constraint violation"}, status=500)
        return _handle_existing_charge(existing, amount, user_id, transaction_uuid)

    logger.info(
        "Payment charge succeeded: transaction_id=%s amount=%s user_id=%s email=%s",
        transaction_uuid,
        amount,
        user_id,
        _mask_email(user_email),
    )
    return _payment_response(payment, status=201)


def _handle_existing_charge(existing, amount, user_id, transaction_uuid):
    """Answer a charge request for a transaction that already has a payment row."""
    if existing.status == Payment.Status.VOIDED:
        logger.warning(
            "Payment charge rejected for voided transaction: transaction_id=%s",
            transaction_uuid,
        )
        return Response(
            {
                "error": "Transaction has been voided",
                "code": "TRANSACTION_VOIDED",
                "status": Payment.Status.NOT_CHARGED,
                "transaction_id": str(transaction_uuid),
            },
            status=409,
        )

    if existing.status == Payment.Status.REFUNDED:
        logger.warning(
            "Payment charge rejected for refunded transaction: transaction_id=%s",
            transaction_uuid,
        )
        return Response(
            {
                "error": "Cannot charge already refunded transaction",
                "transaction_id": str(transaction_uuid),
                "status": Payment.Status.REFUNDED,
            },
            status=409,
        )

    # Replay must match the original amount and user, or it is a conflicting payload
    same_amount = existing.amount == amount
    same_user = (existing.user_id or None) == (user_id or None)
    if not (same_amount and same_user):
        logger.error(
            "Payment charge replay payload mismatch: transaction_id=%s existing_amount=%s new_amount=%s existing_user=%s new_user=%s",
            transaction_uuid,
            existing.amount,
            amount,
            existing.user_id,
            user_id,
        )
        return Response(
            {
                "error": "Idempotency key re-used with conflicting payload",
                "code": "IDEMPOTENCY_PAYLOAD_MISMATCH",
                "transaction_id": str(transaction_uuid),
                "existing_status": existing.status,
            },
            status=409,
        )

    logger.info(
        "Payment charge replay: transaction_id=%s status=%s amount=%s",
        transaction_uuid,
        existing.status,
        existing.amount,
    )
    return _payment_response(existing, status=200)


@api_view(["GET"])
def payment_check(request):
    auth_err = verify_internal_service_key(request)
    if auth_err:
        return auth_err

    transaction_uuid, error = _require_transaction_id(request.query_params.get("transaction_id"))
    if error:
        return error

    payment = Payment.objects.filter(transaction_id=transaction_uuid).first()
    if payment is None:
        return Response({"transaction_id": str(transaction_uuid), "status": Payment.Status.NOT_CHARGED})
    if payment.status == Payment.Status.VOIDED:
        return _voided_response(transaction_uuid)
    return _payment_response(payment, status=200)


@api_view(["POST"])
def void_payment(request):
    auth_err = verify_internal_service_key(request)
    if auth_err:
        return auth_err

    if not isinstance(request.data, Mapping):
        return _bad_request(INVALID_BODY_MESSAGE)

    transaction_uuid, error = _require_transaction_id(request.data.get("transaction_id"))
    if error:
        return error

    with transaction.atomic():
        payment = _lock_payment(transaction_uuid)
        if payment:
            logger.info(
                "Payment void processed for existing transaction: transaction_id=%s status=%s",
                transaction_uuid,
                payment.status,
            )
        else:
            # No row yet: create a VOIDED tombstone so a late charge is rejected
            try:
                with transaction.atomic():
                    Payment.objects.create(
                        transaction_id=transaction_uuid,
                        amount=None,
                        status=Payment.Status.VOIDED,
                    )
                logger.info("Payment void tombstone created: transaction_id=%s", transaction_uuid)
                return _voided_response(transaction_uuid)
            except IntegrityError:
                # Another request created the row first
                payment = _lock_payment(transaction_uuid)
                if payment is None:
                    return Response({"error": "Database integrity constraint violation"}, status=500)
                logger.info(
                    "Payment void race resolved: transaction_id=%s status=%s",
                    transaction_uuid,
                    payment.status,
                )

        if payment.status == Payment.Status.VOIDED:
            return _voided_response(transaction_uuid)
        return _payment_response(payment, status=200)


@api_view(["POST"])
def refund_payment(request):
    auth_err = verify_internal_service_key(request)
    if auth_err:
        return auth_err

    if not isinstance(request.data, Mapping):
        return _bad_request(INVALID_BODY_MESSAGE)

    transaction_uuid, error = _require_transaction_id(request.data.get("transaction_id"))
    if error:
        return error

    with transaction.atomic():
        payment = _lock_payment(transaction_uuid)

        if payment is None:
            logger.warning("Payment refund failed: transaction_id=%s not found", transaction_uuid)
            return Response(
                {"error": "Payment not found", "transaction_id": str(transaction_uuid)},
                status=404,
            )

        # Idempotent: a second refund returns the already-refunded record
        if payment.status == Payment.Status.REFUNDED:
            logger.info("Payment refund replay: transaction_id=%s", transaction_uuid)
            return _payment_response(payment, status=200)

        if payment.status != Payment.Status.CHARGED:
            logger.warning(
                "Payment refund rejected for non-charged state: transaction_id=%s status=%s",
                transaction_uuid,
                payment.status,
            )
            return Response(
                {
                    "error": f"Cannot refund transaction with status {payment.status}",
                    "transaction_id": str(transaction_uuid),
                    "status": payment.status,
                },
                status=400,
            )

        payment.status = Payment.Status.REFUNDED
        payment.refunded_at = timezone.now()
        payment.save(update_fields=["status", "refunded_at", "updated_at"])
        logger.info(
            "Payment refund succeeded: transaction_id=%s amount=%s",
            transaction_uuid,
            payment.amount,
        )
        return _payment_response(payment, status=200)