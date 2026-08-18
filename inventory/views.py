import requests

from rest_framework.decorators import api_view
from rest_framework.response import Response
from requests.exceptions import Timeout, ConnectionError

from django.shortcuts import get_object_or_404
from django.db import transaction, IntegrityError
from django.http import Http404

from .serializers import ProductSerializer
from .models import Product, Reservation

TRANSACTION_STATUS_UPDATE_URL = "http://127.0.0.1:8002/transaction/status/update/{}/"
REQUEST_TIMEOUT = 5


@api_view(["GET"])
def health_check(request):
    return Response({"status": "healthy"})


@api_view(["GET"])
def product_list(request):
    products = Product.objects.all()
    serializer = ProductSerializer(products, many=True)
    return Response(serializer.data)


# reserve_product helpers

def _validate_reserve_input(data):
    # Returns an error Response or None if input is valid
    if not data:
        return Response({"message": "no input values"})

    if not data.get("product_id"):
        return Response({"error": "product_id is required"}, status=400)

    quantity = data.get("quantity")
    if not quantity or quantity <= 0:
        return Response({"error": "Quantity must be greater than 0"}, status=400)

    if not data.get("transaction_id"):
        return Response({"error": "transaction_id is required"}, status=400)

    return None


def _reservation_response(reservation, product, transaction_id):
    """
    Unified response for every successful reserve path.

    Returned by:
      - The first successful reservation (new row created).
      - The fast-path sequential duplicate (filter hit before atomic block).
      - The concurrent duplicate that loses the DB UNIQUE constraint race
        (IntegrityError caught, stock rollback confirmed).

    Fields
    ------
    message        : Human-readable confirmation string.
    transaction_id : Echoed from the request for caller correlation.
    status         : Current Reservation.status value.
    product_id     : The reserved product.
    quantity       : The quantity recorded in this Reservation row.
    remaining      : Current product stock AFTER the reservation was applied.
                     On idempotent paths this reflects the live stock at the
                     time of the duplicate call, which may differ from the
                     original call if other reservations occurred in between.

    The `detail` field (Coordinator callback outcome) is intentionally absent:
      - It is internal infrastructure noise, not a resource attribute.
      - It cannot be reconstructed on idempotent paths without a second HTTP
        call to the Coordinator.
      - No caller (including the Coordinator) reads it.
    """
    return Response({
        "message": "Inventory reserved",
        "transaction_id": str(transaction_id),
        "status": reservation.status,
        "product_id": product.id,
        "quantity": reservation.quantity,
        "remaining": product.stock,
    })


@api_view(["POST"])
def reserve_product(request):
    error_response = _validate_reserve_input(request.data)
    if error_response:
        return error_response

    product_id = request.data.get("product_id")
    quantity = request.data.get("quantity")
    transaction_id = request.data.get("transaction_id")

    existing_reservation = Reservation.objects.filter(transaction_id=transaction_id).first()
    if existing_reservation:  # fast-path idempotency: no DB lock needed
        product = Product.objects.get(id=existing_reservation.product_id)
        return _reservation_response(existing_reservation, product, transaction_id)

    # Race-safe reserve
    # -----------------
    # Two threads can both pass the filter().first() check above before either
    # has committed a row (the check is a plain SELECT, not locked).  When both
    # then enter the atomic block:
    #
    #   Thread A: acquires SELECT FOR UPDATE lock on product, decrements stock,
    #             creates Reservation → commits.
    #   Thread B: waits at SELECT FOR UPDATE until A commits, then reads the
    #             updated (lower) stock, decrements again, tries to create a
    #             Reservation with the same transaction_id → IntegrityError from
    #             the DB UNIQUE constraint on inventory_reservation.transaction_id.
    #
    # When the atomic block raises, Django automatically rolls it back — including
    # Thread B's stock decrement.  We catch IntegrityError *outside* the block
    # so the connection is in a clean state and the re-fetch .get() can run.
    #
    # This is identical to the pattern used in Payment.charge_payment().
    try:
        # transaction.atomic(): __enter__ runs BEGIN before the block executes;
        # __exit__ runs COMMIT if the block finished without raising, or
        # ROLLBACK-then-reraise if it didn't. A plain `return` from inside the
        # block still exits normally, so it still commits — including the
        # early "Not enough stock" return below (harmless: nothing was
        # written yet at that point).
        with transaction.atomic():
            product = get_object_or_404(Product.objects.select_for_update(), id=product_id)

            if product.stock < quantity:
                return Response({"error": "Not enough stock"}, status=400)

            product.stock -= quantity
            product.save()

            reservation = Reservation.objects.create(
                product=product, quantity=quantity, transaction_id=transaction_id,
            )

    except IntegrityError:
        # A concurrent request already created a Reservation for this
        # transaction_id and committed before us.  Re-fetch and return the
        # existing row — the stock decrement above was rolled back automatically.
        existing = Reservation.objects.get(transaction_id=transaction_id)
        product = Product.objects.get(id=existing.product_id)
        return _reservation_response(existing, product, transaction_id)

    # Reservation is already committed at this point.  This call is
    # best-effort notification to the Coordinator, not part of the DB
    # transaction. If it fails, the reservation still stands; we just
    # let it pass silently — the outcome is not exposed in the response
    # because it cannot be reconstructed on idempotent paths.
    try:
        requests.get(
            TRANSACTION_STATUS_UPDATE_URL.format(transaction_id),
            timeout=REQUEST_TIMEOUT,
        )
    except (Timeout, ConnectionError):
        pass  # non-fatal; reservation is already committed

    return _reservation_response(reservation, product, transaction_id)



# release / check

# used for compensation
@api_view(["POST"])
def release_product(request):
    transaction_id = request.data.get("transaction_id")

    if transaction_id == "fail-release":
        return Response({"error": "Release failed intentionally"}, status=500)

    try:
        with transaction.atomic():
            reservation = get_object_or_404(
                Reservation.objects.select_for_update(),
                transaction_id=transaction_id,
            )

            if reservation.status == Reservation.Status.RELEASED:
                return Response({
                    "message": "Reservation already released",
                    "transaction_id": transaction_id,
                })

            product = Product.objects.select_for_update().get(id=reservation.product_id)
            product.stock += reservation.quantity
            product.save()

            reservation.status = Reservation.Status.RELEASED
            reservation.save()
    except Http404:
        # Override get_object_or_404's default "detail" message.
        return Response({
            "transaction_id": transaction_id,
            "error": "Reservation not found",
        }, status=404)

    return Response({"message": "Inventory released", "transaction_id": transaction_id})


# used for compensation check
@api_view(["GET"])
def reservation_check(request):
    transaction_id = request.query_params.get("transaction_id")

    if not transaction_id:
        return Response({"error": "transaction_id is required"}, status=400)

    try:
        reservation = get_object_or_404(Reservation, transaction_id=transaction_id)
    except Http404:
        return Response({
            "transaction_id": transaction_id,
            "status": Reservation.Status.NOT_RESERVED,
        })

    return Response({"transaction_id": transaction_id, "status": reservation.status})