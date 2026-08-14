from rest_framework.decorators import api_view
from rest_framework.response import Response

from django.shortcuts import get_object_or_404
from django.db import transaction
from .serializers import ProductSerializer
from .models import Product, Reservation

from django.http import Http404

import requests


@api_view(["GET"])
def health_check(request):
    return Response({
        "status": "healthy",
    })

@api_view(["GET"])
def product_list(request):

    products = Product.objects.all()
    serializer = ProductSerializer(products, many=True)

    return Response(serializer.data)

@api_view(["POST"])
def reserve_product(request):

    if not request.data: # {}
        return Response({
            "message": "no input values"
        })

    product_id = request.data.get("product_id")
    quantity = request.data.get("quantity")
    transaction_id = request.data.get("transaction_id")

    if not product_id:
        return Response(
            {"error": "product_id is required"},
            status=400
        )

    if not quantity or quantity <= 0:
        return Response(
            {"error": "Quantity must be greater than 0"},
            status=400
        )

    if not transaction_id:
        return Response(
            {"error": "transaction_id is required"},
            status=400,
        )

    existing_reservation = Reservation.objects.filter(
        transaction_id=transaction_id
    ).first()

    if existing_reservation: # idempotency
        return Response({
            "message": "Reservation already exists",
            "transaction_id": transaction_id,
            "status": existing_reservation.status
        })

    with transaction.atomic(): # <- __enter__ called BEFORE this block starts
        product = get_object_or_404(
            Product.objects.select_for_update(),
            id=product_id
        )

        if product.stock < quantity:
            return Response(
                {"error": "Not enough stock"},
                status=400
            )

        product.stock -= quantity
        product.save()

        Reservation.objects.create(
            product=product,
            quantity=quantity,
            transaction_id=transaction_id
        )
    """
    with knows nothing about databases, commits, or rollbacks.
    It only knows: call __enter__ "before the block", 
    and ALWAYS call __exit__ after the block — whether the 
    block succeeded or raised an exception (passing exit the 
    error info if there was one).

    transaction.atomic() supplies what __enter__ and __exit__ 
    actually DO:
        - __enter__  → BEGIN the transaction
        - __exit__   → check if it was told about an error:
                        no error  → COMMIT
                        error     → ROLLBACK, then re-raise
    """ 
    """
    Python sees with transaction.atomic():

    Calls __enter__() — this runs BEGIN on the DB — before 
    executing any line inside the indented block("that before the block")

    Runs the indented lines (product.save(), Reservation.objects.create(), etc.)

    Once those lines finish (either normally or via exception), calls __exit__()
    — this runs COMMIT or ROLLBACK(respective of the scenario)
    """

    update_transaction = requests.post(
            "http://127.0.0.1:8002/transaction/status/update/",
            json={
                "transaction_id": transaction_id,
            },
            timeout=5,
        )

    if update_transaction.status == 200:
        return Response({
            "message": "Inventory reserved",
            "product_id": product.id,
            "quantity": quantity,
            "remaining": product.stock,
            "detail": "Transaction update success"
        })


    return Response({
        "message": "Inventory reserved",
        "product_id": product.id,
        "quantity": quantity,
        "remaining": product.stock,
        "detail": "transaction update fail"
    })

# used for compensation
@api_view(["POST"])
def release_product(request):
    transaction_id = request.data.get("transaction_id")


    if transaction_id == "fail-release":
        return Response(
            {"error": "Release failed intentionally"},
            status=500
        )

    try:
        with transaction.atomic():
            reservation = get_object_or_404(
                Reservation.objects.select_for_update(),
                transaction_id=transaction_id
            )
    
            if reservation.status == Reservation.Status.RELEASED:
                return Response({
                    "message": "Reservation already released",
                    "transaction_id": transaction_id,
                })
    
            product = Product.objects.select_for_update().get(
                id=reservation.product_id
            )
    
            product.stock += reservation.quantity
            product.save()
    
            reservation.status = Reservation.Status.RELEASED
            reservation.save()

    # we overide the default "detail" exception message of get_objet_404()
    except Http404:
        return Response({
            "transaction_id": transaction_id,
            "error": "Reservation not found",
        }, status=404)

    return Response({
        "message": "Inventory released",
        "transaction_id": transaction_id
    })

@api_view(["GET"])
def reservation_check(request):
    transaction_id = request.query_params.get("transaction_id")


    if not transaction_id:
        return Response(
            {"error": "transaction_id is required"},
            status=400
        )

    try:
        reservation = get_object_or_404(
            Reservation,
            transaction_id=transaction_id
        )
    except Http404:
        return Response({
            "transaction_id": transaction_id,
            "status": Reservation.Status.NOT_RESERVED,
        })

    return Response({
        "transaction_id": transaction_id,
        "status": reservation.status,
    })
    

    