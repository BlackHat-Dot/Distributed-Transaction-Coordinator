import requests

from rest_framework.decorators import api_view
from rest_framework.response import Response
from requests.exceptions import Timeout, ConnectionError

from django.shortcuts import get_object_or_404
from django.db import transaction
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


def _reservation_response(product, quantity, detail):
    return Response({
        "message": "Inventory reserved",
        "product_id": product.id,
        "quantity": quantity,
        "remaining": product.stock,
        "detail": detail,
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
    if existing_reservation:  # idempotency
        return Response({
            "message": "Reservation already exists",
            "transaction_id": transaction_id,
            "status": existing_reservation.status,
        })

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

        Reservation.objects.create(product=product, quantity=quantity, transaction_id=transaction_id)

    # Reservation is already committed at this point , this call is
    # best-effort notification to the Coordinator, not part of the DB
    # transaction. If it fails, the reservation still stands; we just
    # report that the notify step didn't succeed, instead of letting an
    # unhandled exception turn a successful reservation into a 500.
    try:
        update_transaction = requests.get(
            TRANSACTION_STATUS_UPDATE_URL.format(transaction_id),
            timeout=REQUEST_TIMEOUT,
        )
        detail = "Transaction update success" if update_transaction.status_code == 200 else "transaction update fail"
    except (Timeout, ConnectionError):
        detail = "transaction update fail"

    return _reservation_response(product, quantity, detail)


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