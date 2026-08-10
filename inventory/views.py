from django.shortcuts import render
from django.http import JsonResponse
from .models import Product

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
