import uuid
import requests

from rest_framework.decorators import api_view
from rest_framework.response import Response
from requests.exceptions import Timeout, ConnectionError

from .models import Transaction


@api_view(["POST"])
def create_transaction(request):
    transaction_id = str(uuid.uuid4()) # random uuid
    product_id = request.data.get("product_id", 1)
    quantity = request.data.get("quantity", 1)
    amount = request.data.get("amount", 500)

    Transaction.objects.create(
        transaction_id=transaction_id,
        status="STARTED",
        product_id=product_id,
        quantity=quantity,
        amount=amount
    )

    try:
        response = requests.post(
            "http://127.0.0.1:8000/products/reserve/",
            json={
                "product_id": product_id,
                "quantity": quantity,
                "transaction_id": transaction_id,
            },
            timeout=5,
    )
    except (Timeout, ConnectionError) as e:
        if isinstance(e, Timeout):
            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status="INVENTORY_TIMEOUT")

            return Response({
                "transaction_id": transaction_id,
                "status": "INVENTORY_TIMEOUT",
            }, status=504)
        elif isinstance(e, ConnectionError):
            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status="INVENTORY_CONNECTION_ERROR")

            return Response({
                "transaction_id": transaction_id,
                "status": "INVENTORY_CONNECTION_ERROR",
            }, status=504
            )

    # timeout
        # Connection established
        #        ↓
        # Request sent
        #        ↓
        # No response within 5 seconds

    # ConnectionError
        # Tried to connect
        #         ↓
        # Could not establish connection

    # for example: coordinator -> http://127.0.0.1:8000 is not running
    # then, connection won't happen, so ConnectionError raised

    if response.status_code != 200:
        Transaction.objects.filter(
            transaction_id=transaction_id
        ).update(status="INVENTORY_RESERVE_FAILED")
        
        return Response({
            "transaction_id": transaction_id,
            "error": "Inventory reservation failed",
            "inventory_response": response.json()
            }, status=400)

    Transaction.objects.filter(
        transaction_id=transaction_id
    ).update(status="INVENTORY_RESERVED")

    Transaction.objects.filter(
        transaction_id=transaction_id
    ).update(status="PAYMENT_PROCESSING")

    amount = request.data.get("amount", 0) # 9999

    fail_compensation = request.data.get("fail_compensation", False)

    try:
        payment_response = requests.post(
            "http://127.0.0.1:8001/payments/charge/",
            json={
                "transaction_id": transaction_id,
                "amount": amount,
            },
            timeout=5,
    )
    except (Timeout, ConnectionError) as e:
        if isinstance(e, Timeout):
            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status="PAYMENT_TIMEOUT")
        
        elif isinstance(e, ConnectionError):
            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status="PAYMENT_CONNECTION_ERROR")

    if payment_response.status_code != 200:

        Transaction.objects.filter(
                    transaction_id=transaction_id
                ).update(status="COMPENSATING")
        
        release_transaction_id = "fail-release" if fail_compensation else transaction_id

        try:
            release_response = requests.post(
                "http://127.0.0.1:8000/products/release/",
                json={
                    "transaction_id": release_transaction_id,
                },
                timeout=5,
            )
        except (Timeout, ConnectionError) as e:
            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status="COMPENSATION_FAILED")

            if isinstance(e, Timeout):
                return Response({
                    "transaction_id": transaction_id,
                    "status": "COMPENSATION_FAILED",
                    "cause": "TIMEOUT",
                    "error": "Could not reach Inventory during compensation",
                }, status=503)
            
            elif isinstance(e, ConnectionError):
                return Response({
                    "transaction_id": transaction_id,
                    "status": "COMPENSATION_FAILED",
                    "cause": "CONNECTION_FAILED",
                    "error": "Could not reach Inventory during compensation",
                }, status=503)

        if release_response.status_code == 200:
            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status="COMPENSATED")
        else:
            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status="COMPENSATION_FAILED")

        current_transaction = Transaction.objects.get(
            transaction_id=transaction_id
        )

        return Response({
            "transaction_id": transaction_id,
            "status": current_transaction.status,
            "payment_response": payment_response.json(),
            "compensation_response": release_response.json(),
        }, status=400)

    Transaction.objects.filter(
        transaction_id=transaction_id
    ).update(status="COMPLETED")
    
    return Response({
        "transaction_id": transaction_id,
        "status": "COMPLETED",
        "product_id": product_id,
        "quantity": quantity,
        "amount": amount,
    })

@api_view(["POST"])
def retry_compensation(request, transaction_id):
    transaction = Transaction.objects.get(
        transaction_id=transaction_id
    )

    if transaction.status == "COMPENSATED":
        return Response({
            "transaction_id": str(transaction_id),
            "status": "COMPENSATED",
            "message": "Transaction is already compensated",
        })

    try:
        release_response = requests.post(
            "http://127.0.0.1:8000/products/release/",
            json={
                "transaction_id": str(transaction_id),
            },
            timeout=5,
        )
    except (Timeout, ConnectionError) as e:
        Transaction.objects.filter(
            transaction_id=transaction_id
        ).update(status="RETRY_COMPENSATION_FAILED")
        if isinstance(e, Timeout):
            return Response({
                "transaction_id": transaction_id,
                "status": "RETRY_COMPENSATION_FAILED",
                "cause": "TIMEOUT",
                "error": "Could not reach Inventory during retry compensation",
            }, status=503)
        
        elif isinstance(e, ConnectionError):
            return Response({
                "transaction_id": transaction_id,
                "status": "RETRY_COMPENSATION_FAILED",
                "cause": "CONNECTION_FAILED",
                "error": "Could not reach Inventory during retry compensation",
            }, status=503)

    if release_response.status_code == 200:
        transaction.status = "COMPENSATED_ON_RETRY"
        transaction.save(update_fields=["status"])
        # Django updates only the status column 
        # instead of saving every field on the Transaction object

        return Response({
            "transaction_id": transaction_id,
            "status": "COMPENSATED_ON_RETRY",
            "compensation_response": release_response.json(),
        })

    return Response({
        "transaction_id": transaction_id,
        "status": "COMPENSATION_FAILED_ON_RETRY",
        "compensation_response": release_response.json(),
    }, status=500)

@api_view(["GET"])
def get_transaction(request, transaction_id):
    transaction = Transaction.objects.get(
        transaction_id=transaction_id
    )

    return Response({
        "transaction_id": str(transaction.transaction_id),
        "status": transaction.status,
        "product_id": transaction.product_id,
        "quantity": transaction.quantity,
        "amount": str(transaction.amount),
        "created_at": transaction.created_at,
        "updated_at": transaction.updated_at,
    })

@api_view(["GET"])
def list_transactions(request):
    transactions = Transaction.objects.all().order_by("-created_at")

    return Response([
        {
            "transaction_id": str(transaction.transaction_id),
            "status": transaction.status,
            "product_id": transaction.product_id,
            "quantity": transaction.quantity,
            "amount": str(transaction.amount),
            "created_at": transaction.created_at,
            "updated_at": transaction.updated_at,
        }
        for transaction in transactions
    ])

@api_view(["GET"])
def update_status(request, transaction_id):
    pass