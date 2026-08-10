from rest_framework.decorators import api_view
from rest_framework.response import Response

from django.http import JsonResponse
from .serializers import ProductSerializer
from .models import Product, Reservation

def product_list(request):
    products = Product.objects.all()

    data = []

    for product in products:
        data.append({
            "id": product.id,
            "name": product.name,
            "stock": product.stock
        })

    return JsonResponse(data, safe=False)
    # safe = False, becuase we're saying to Django
    # that the "data" is list and you can return
    # it has JSON

@api_view(["GET"])
def product_list(request):
    products = Product.objects.all()
    serializer = ProductSerializer(products, many=True)

    return Response(serializer.data)

@api_view(["POST"])
def reserve_product(request):
    product_id = request.data.get("product_id")
    quantity = request.data.get("quantity")

    product = Product.objects.get(id=product_id)

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
        transaction_id="test-transaction",
    )

    return Response({
        "message": "Inventory reserved",
        "product_id": product.id,
        "quantity": quantity,
        "remaining": product.stock
    })
    # not exactly reservation but a vague approach.