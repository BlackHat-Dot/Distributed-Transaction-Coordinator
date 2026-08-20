from rest_framework.decorators import api_view
from rest_framework.response import Response

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


def _validate_reserve_input(data):
    if not data:
        return Response({"message": "no input values"}, status=400)

    if not data.get("product_id"):
        return Response({"error": "product_id is required"}, status=400)

    quantity = data.get("quantity")
    if not isinstance(quantity, int):
        return Response({"error": "Quantity must be an integer"}, status=400)

    if quantity <= 0:
        return Response({"error": "Quantity must be greater than 0"}, status=400)

    if not data.get("transaction_id"):
        return Response({"error": "transaction_id is required"}, status=400)

    return None


def _reservation_response(reservation, product, transaction_id):

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
    if existing_reservation:
        product = Product.objects.get(id=existing_reservation.product_id)
        return _reservation_response(existing_reservation, product, transaction_id)

    
    try:
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
    
        existing = Reservation.objects.get(transaction_id=transaction_id)
        product = Product.objects.get(id=existing.product_id)
        return _reservation_response(existing, product, transaction_id)


    return _reservation_response(reservation, product, transaction_id)



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
        return Response({
            "transaction_id": transaction_id,
            "error": "Reservation not found",
        }, status=404)

    return Response({"message": "Inventory released", "transaction_id": transaction_id})


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