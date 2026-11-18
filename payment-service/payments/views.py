# verified

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

from .middleware import set_correlation_id
from .models import Payment
from .permissions import verify_internal_service_key

logger = logging.getLogger("payments")


def _mask_email(email):
    """
    Mask email address for privacy-safe logging (e.g. j***e@example.com).
    Returns None if email is missing or malformed.
    """
    # Guards against non-string, empty, or unparseable email payloads to prevent unhandled exceptions during string splitting.
    if not email or not isinstance(email, str) or "@" not in email:
        return None
    user, domain = email.split("@", 1)
    # Obfuscates the local part of the address while retaining boundary characters and full domain for diagnostics.
    if len(user) <= 1:
        masked_user = user + "***"
    else:
        masked_user = user[0] + "***" + user[-1]
    return f"{masked_user}@{domain}"


# Sentinel amount used during test runs to intentionally simulate downstream gateway payment failure
TESTING_FAILURE_AMOUNT = Decimal("9999")

# Currency quantizer constant used to enforce exact two-decimal precision for monetary transactions
TWO_PLACES = Decimal("0.01")


def _get_max_amount():
    """Derive upper payment bound dynamically from Payment model field definition."""
    # Introspects model metadata to dynamically calculate the maximum allowable numeric value supported by the database column schema.
    field = Payment._meta.get_field("amount")
    max_int_digits = field.max_digits - field.decimal_places
    return Decimal("9" * max_int_digits + "." + "9" * field.decimal_places)


MAX_PAYMENT_AMOUNT = _get_max_amount()


@api_view(["GET"])
def health_check(request):
    """Public health probe for service liveness."""
    # Returns immediate 200 OK probe for container orchestrators and ingress load balancers to monitor service availability.
    return Response({"status": "healthy", "service": "payment"})


def _validate_uuid(value):
    """Safely validate UUID string or object."""
    # Safely parses string representations into native UUID objects, catching conversion failures without crashing.
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return None


def _require_transaction_id(raw_value):
    """Validate presence and UUID format of transaction_id."""
    # Enforces mandatory transaction identifier presence before delegating to format validation.
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        return None, Response({"error": "transaction_id is required"}, status=400)

    # Verifies that the client-supplied transaction_id adheres strictly to RFC 4122 UUID formatting.
    transaction_uuid = _validate_uuid(raw_value)
    if not transaction_uuid:
        return None, Response({"error": "transaction_id must be a valid UUID"}, status=400)

    return transaction_uuid, None


def _validate_amount(raw_amount):
    """Validate payment amount precision, finiteness, positivity, and bounds."""
    # Enforces mandatory charge amount specification on all incoming payment requests.
    if raw_amount is None:
        return None, Response({"error": "amount is required"}, status=400)

    # Converts arbitrary numeric or string representations to exact-precision Decimal instances to prevent floating-point inaccuracies.
    try:
        amount = Decimal(str(raw_amount))
    except (InvalidOperation, TypeError):
        return None, Response({"error": "amount must be a valid number"}, status=400)

    # Rejects NaN, Infinity, and subnormal numeric representations to protect accounting integrity.
    if not amount.is_finite():
        return None, Response({"error": "amount must be a finite number"}, status=400)

    # F7: Reject inputs with more than 2 decimal places (do not silently round)
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        return None, Response({"error": "amount cannot have more than 2 decimal places"}, status=400)

    # Enforces strictly positive transaction value to prevent zero or negative charges.
    if amount <= 0:
        return None, Response({"error": "amount must be greater than zero"}, status=400)

    # F8: Upper bound check derived dynamically from Payment model field definition
    if amount > MAX_PAYMENT_AMOUNT:
        return None, Response(
            {"error": f"amount exceeds maximum allowable limit of {MAX_PAYMENT_AMOUNT}"},
            status=400,
        )

    # Quantizes valid amounts to exactly two decimal places matching fractional currency standards.
    return amount.quantize(TWO_PLACES), None


def _validate_user_info(data):
    """Validate optional user_id and user_email formatting and length limits."""
    user_id = None
    # Validates client-supplied customer identifier and enforces column length constraints.
    if "user_id" in data and data["user_id"] is not None:
        raw_uid = data["user_id"]
        uid_str = str(raw_uid).strip() if not isinstance(raw_uid, bool) else str(raw_uid)
        if uid_str != "":
            max_uid_len = Payment._meta.get_field("user_id").max_length
            if len(uid_str) > max_uid_len:
                return None, None, Response(
                    {"error": f"user_id exceeds maximum length of {max_uid_len}"},
                    status=400,
                )
            user_id = uid_str

    user_email = None
    # Validates customer email format against RFC standards and model character constraints.
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
    """Format standardized payment response dictionary."""
    # Serializes payment model records into standardized, contract-compliant JSON response payloads.
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
    """Execute idempotent charge against payment gateway / ledger."""
    # Verifies inter-service authentication before processing any payment execution commands.
    auth_err = verify_internal_service_key(request)
    if auth_err:
        return auth_err

    # Enforces that incoming HTTP payload is a well-formed JSON object mapping rather than primitive data.
    if not isinstance(request.data, Mapping):
        return Response({"error": "Request body must be a valid JSON object"}, status=400)

    transaction_uuid, error_response = _require_transaction_id(request.data.get("transaction_id"))
    if error_response:
        return error_response
    # Binds the validated transaction UUID to the thread-local correlation context for distributed tracing.
    set_correlation_id(str(transaction_uuid))

    amount, error_response = _validate_amount(request.data.get("amount"))
    if error_response:
        return error_response

    user_id, user_email, error_response = _validate_user_info(request.data)
    if error_response:
        return error_response

    # Feature flag protecting synthetic payment failure injection during testing and integration runs.
    # Payment decline flag (F2: gated behind ENABLE_TEST_PAYMENT_FAILURE setting)
    enable_test_failure = getattr(settings, "ENABLE_TEST_PAYMENT_FAILURE", False) or getattr(settings, "IS_TESTING", False)
    if enable_test_failure and amount == TESTING_FAILURE_AMOUNT:
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
        if getattr(settings, "IS_TESTING", False) or getattr(settings, "DEBUG", False):
            response_data["_testing"] = "Intentional failure for compensation testing"
        return Response(response_data, status=422)

    # Queries database for existing transaction record to support safe, idempotent charge replays.
    existing = Payment.objects.filter(transaction_id=transaction_uuid).first()
    if existing:
        return _handle_existing_charge(existing, amount, user_id, transaction_uuid, user_email=user_email)

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
        return _handle_existing_charge(existing, amount, user_id, transaction_uuid, user_email=user_email)

    logger.info(
        "Payment charge succeeded: transaction_id=%s amount=%s user_id=%s email=%s",
        transaction_uuid,
        amount,
        user_id,
        _mask_email(user_email),
    )
    return _payment_response(payment, status=201)


def _handle_existing_charge(existing, amount, user_id, transaction_uuid, user_email=None):
    """Handle replay of existing charge, validating idempotent payload match."""
    # F4: Attempt to charge a VOIDED transaction => 409
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

    # F14: Charge replay on a REFUNDED payment => 409
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

    # F3: On replay, compare normalized amount and user_id; mismatch => 409
    # A3 contract: Must NOT contain top-level status or payment dict
    user_mismatch = (existing.user_id or None) != (user_id or None)
    amount_mismatch = (existing.amount != amount)

    email_mismatch = False
    if user_email is not None and existing.user_email is not None:
        email_mismatch = (str(existing.user_email).strip().lower() != str(user_email).strip().lower())

    if amount_mismatch or user_mismatch or email_mismatch:
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
    """Query current payment state without side effects."""
    auth_err = verify_internal_service_key(request)
    if auth_err:
        return auth_err

    raw_tid = getattr(request, "query_params", {}).get("transaction_id")
    transaction_uuid, error_response = _require_transaction_id(raw_tid)
    if error_response:
        return error_response
    # Binds the validated transaction UUID to the thread-local correlation context for distributed tracing.
    set_correlation_id(str(transaction_uuid))

    payment = Payment.objects.filter(transaction_id=transaction_uuid).first()
    if payment:
        if payment.status == Payment.Status.VOIDED:
            return Response(
                {
                    "transaction_id": str(transaction_uuid),
                    "status": Payment.Status.NOT_CHARGED,
                    "voided": True,
                },
                status=200,
            )
        return _payment_response(payment, status=200)

    return Response({"transaction_id": str(transaction_uuid), "status": Payment.Status.NOT_CHARGED})


@api_view(["POST"])
def void_payment(request):
    """Void a transaction to prevent it from ever being charged."""
    auth_err = verify_internal_service_key(request)
    if auth_err:
        return auth_err

    # Enforces that incoming HTTP payload is a well-formed JSON object mapping rather than primitive data.
    if not isinstance(request.data, Mapping):
        return Response({"error": "Request body must be a valid JSON object"}, status=400)

    transaction_uuid, error_response = _require_transaction_id(request.data.get("transaction_id"))
    if error_response:
        return error_response
    # Binds the validated transaction UUID to the thread-local correlation context for distributed tracing.
    set_correlation_id(str(transaction_uuid))

    with transaction.atomic():
        p = Payment.objects.select_for_update().filter(transaction_id=transaction_uuid).first()
        if p:
            logger.info(
                "Payment void processed for existing transaction: transaction_id=%s status=%s",
                transaction_uuid,
                p.status,
            )
            if p.status == Payment.Status.VOIDED:
                return Response(
                    {
                        "transaction_id": str(transaction_uuid),
                        "status": Payment.Status.NOT_CHARGED,
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
                logger.info(
                    "Payment void tombstone created: transaction_id=%s",
                    transaction_uuid,
                )
        except IntegrityError:
            p = Payment.objects.select_for_update().filter(transaction_id=transaction_uuid).first()
            if not p:
                return Response({"error": "Database integrity constraint violation"}, status=500)
            logger.info(
                "Payment void race resolved: transaction_id=%s status=%s",
                transaction_uuid,
                p.status,
            )
            if p.status == Payment.Status.VOIDED:
                return Response(
                    {
                        "transaction_id": str(transaction_uuid),
                        "status": Payment.Status.NOT_CHARGED,
                        "voided": True,
                    },
                    status=200,
                )
            return _payment_response(p, status=200)

    return Response(
        {
            "transaction_id": str(transaction_uuid),
            "status": Payment.Status.NOT_CHARGED,
            "voided": True,
        },
        status=200,
    )


@api_view(["POST"])
def refund_payment(request):
    """Refund an existing charged payment with atomic row locking."""
    auth_err = verify_internal_service_key(request)
    if auth_err:
        return auth_err

    # Enforces that incoming HTTP payload is a well-formed JSON object mapping rather than primitive data.
    if not isinstance(request.data, Mapping):
        return Response({"error": "Request body must be a valid JSON object"}, status=400)

    transaction_uuid, error_response = _require_transaction_id(request.data.get("transaction_id"))
    if error_response:
        return error_response
    # Binds the validated transaction UUID to the thread-local correlation context for distributed tracing.
    set_correlation_id(str(transaction_uuid))

    # F20: Single lookup under select_for_update inside one atomic block
    with transaction.atomic():
        p = Payment.objects.select_for_update().filter(transaction_id=transaction_uuid).first()

        if not p:
            logger.warning(
                "Payment refund failed: transaction_id=%s not found",
                transaction_uuid,
            )
            return Response(
                {
                    "error": "Payment not found",
                    "transaction_id": str(transaction_uuid),
                },
                status=404,
            )

        # Idempotent: multiple refunds for the same transaction return the existing refunded record
        if p.status == Payment.Status.REFUNDED:
            logger.info(
                "Payment refund replay: transaction_id=%s",
                transaction_uuid,
            )
            return _payment_response(p, status=200)

        # Reject refund for transactions that were not charged
        if p.status != Payment.Status.CHARGED:
            logger.warning(
                "Payment refund rejected for non-charged state: transaction_id=%s status=%s",
                transaction_uuid,
                p.status,
            )
            return Response(
                {
                    "error": f"Cannot refund transaction with status {p.status}",
                    "transaction_id": str(transaction_uuid),
                    "status": p.status,
                },
                status=400,
            )

        p.status = Payment.Status.REFUNDED
        p.refunded_at = timezone.now()
        p.save(update_fields=["status", "refunded_at", "updated_at"])
        logger.info(
            "Payment refund succeeded: transaction_id=%s amount=%s",
            transaction_uuid,
            p.amount,
        )
        return _payment_response(p, status=200)