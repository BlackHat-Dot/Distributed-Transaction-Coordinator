import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.db import IntegrityError, transaction

from .models import Payment


# Amount that triggers intentional failure for compensation testing
# reserve → payment fails → compensate.
TESTING_FAILURE_AMOUNT = Decimal("9999")

TWO_PLACES = Decimal("0.01")

NOT_CHARGED = "NOT_CHARGED"


def _validate_uuid(value):
    """Return a uuid.UUID if valid, else None."""
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


@api_view(["POST"])
def charge_payment(request):
    raw_transaction_id = request.data.get("transaction_id")
    raw_amount = request.data.get("amount")

    # Validate transaction_id
    if not raw_transaction_id:
        return Response(
            {"error": "transaction_id is required"},
            status=400,
        )

    transaction_uuid = _validate_uuid(raw_transaction_id)
    if transaction_uuid is None:
        return Response(
            {"error": "transaction_id must be a valid UUID"},
            status=400,
        )

    # ── Validate amount ──
    if raw_amount is None:
        return Response(
            {"error": "amount is required"},
            status=400,
        )

    try:
        # Convert through str to avoid float→Decimal imprecision
        amount = Decimal(str(raw_amount))
    except (InvalidOperation, TypeError):
        return Response(
            {"error": "amount must be a valid number"},
            status=400,
        )

    amount = amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    if amount <= 0:
        return Response(
            {"error": "amount must be greater than zero"},
            status=400,
        )

    # ── Intentional failure for testing compensation path ──
    # When the Coordinator sends amount=9999, Payment returns a business
    # failure WITHOUT creating a Payment row, so check_payment correctly
    # reports NOT_CHARGED and the Coordinator compensates Inventory.
    if amount == TESTING_FAILURE_AMOUNT:
        return Response(
            {
                "error": "Payment declined",
                "transaction_id": str(transaction_uuid),
                "status": NOT_CHARGED,
                "_testing": "Intentional failure for compensation testing",
            },
            status=422,
        )

    # ── Idempotent charge with concurrency safety ──
    #
    # Race scenario:
    #   Thread A: filter → None     Thread B: filter → None
    #   Thread A: create → OK       Thread B: create → IntegrityError
    #
    # The unique constraint on transaction_id guarantees at most one row.
    # We catch IntegrityError on the losing thread and re-fetch.

    existing = Payment.objects.filter(
        transaction_id=transaction_uuid,
    ).first()

    if existing is not None:
        return Response({
            "transaction_id": str(existing.transaction_id),
            "amount": str(existing.amount),
            "status": existing.status,
        })

    try:
        with transaction.atomic():
            payment = Payment.objects.create(
                transaction_id=transaction_uuid,
                amount=amount,
                status=Payment.Status.CHARGED,
            )
    except IntegrityError:
        # Concurrent request already created this payment
        existing = Payment.objects.get(transaction_id=transaction_uuid)
        return Response({
            "transaction_id": str(existing.transaction_id),
            "amount": str(existing.amount),
            "status": existing.status,
        })

    return Response(
        {
            "transaction_id": str(payment.transaction_id),
            "amount": str(payment.amount),
            "status": payment.status,
        },
        status=201,
    )


@api_view(["GET"])
def payment_check(request):
    raw_transaction_id = request.query_params.get("transaction_id")

    if not raw_transaction_id:
        return Response(
            {"error": "transaction_id is required"},
            status=400,
        )

    transaction_uuid = _validate_uuid(raw_transaction_id)
    if transaction_uuid is None:
        return Response(
            {"error": "transaction_id must be a valid UUID"},
            status=400,
        )

    payment = Payment.objects.filter(
        transaction_id=transaction_uuid,
    ).first()

    if payment is not None:
        return Response({
            "transaction_id": str(payment.transaction_id),
            "status": payment.status,
        })

    # No payment row → the charge never completed successfully.
    # This is an expected outcome, not an error.
    return Response({
        "transaction_id": str(transaction_uuid),
        "status": NOT_CHARGED,
    })