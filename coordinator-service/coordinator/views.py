import uuid
import requests

from rest_framework.decorators import api_view
from rest_framework.response import Response



@api_view(["POST"])
def create_transaction(request):
    transaction_id = str(uuid.uuid4()) # random uuid

    response = requests.post(
        "http://127.0.0.1:8000/products/reserve/",
        json={
            "product_id": 1,
            "quantity": 1,
            "transaction_id": transaction_id
        },
    )

    return Response({
    "transaction_id": transaction_id,
    "status_code": response.status_code,
    "inventory_response": response.json(),
    })