import uuid
import requests

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Transaction

@api_view(["POST"])
def create_transaction(request):
    transaction_id = str(uuid.uuid4()) # random uuid

    Transaction.objects.create(
        transaction_id=transaction_id,
        status="STARTED",
    )

    response = requests.post(
        "http://127.0.0.1:8000/products/reserve/",
        json={
            "product_id": 1,
            "quantity": 1,
            "transaction_id": transaction_id
        },
    )

    if response.status_code != 200:
        return Response({
            "transaction_id": transaction_id,
            "error": "Inventory reservation failed",
            "inventory_response": response.json()
        }, status=400)

    Transaction.objects.filter(
        transaction_id=transaction_id
    ).update(status="INVRNTORY_RESERVED")

    Transaction.objects.filter(
        transaction_id=transaction_id
    ).update(status="PAYMENT_PROCESSING")

    amount = request.data.get("amount", 0) # 9999

    fail_compensation = request.data.get("fail_compensation", False)

    payment_response = requests.post(
        "http://127.0.0.1:8001/payments/charge/",
        json={
            "transaction_id": transaction_id,
            "amount": amount,
        },
    )

    if payment_response.status_code != 200:
        Transaction.objects.filter(
            transaction_id=transaction_id
        ).update(status="PAYMENT_PROCESSING")

        Transaction.objects.filter(
                    transaction_id=transaction_id
                ).update(status="COMPENSATING")
        
        release_transaction_id = "fail-release" if fail_compensation else transaction_id

        release_response = requests.post(
            "http://127.0.0.1:8000/products/release/",
            json={
                "transaction_id": release_transaction_id,
            },
        )

        if release_response.status_code == 200:
            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status="COMPENSATED")
        else:
            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status="COMPENSATION_FAILED")

        return Response({
            "transaction_id": transaction_id,
            "status": "FAILED",
            "payment_response": payment_response.json(),
            "compensation_response": release_response.json(),
        }, status=400)

    Transaction.objects.filter(
        transaction_id=transaction_id
    ).update(status="COMPLETED")
    
    return Response({
        "transaction_id": transaction_id,
        "status": "COMPLETED",
        "inventory_response": response.json(),
        "payment_response": payment_response.json(),
    })