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
        status=Transaction.Status.STARTED,
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
            ).update(status=Transaction.Status.INVENTORY_TIMEOUT)

            return Response({
                "transaction_id": transaction_id,
                "status": Transaction.Status.INVENTORY_TIMEOUT,
            }, status=504)
        elif isinstance(e, ConnectionError):
            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status=Transaction.Status.INVENTORY_CONNECTION_ERROR)

            return Response({
                "transaction_id": transaction_id,
                "status": Transaction.Status.INVENTORY_CONNECTION_ERROR,
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
        ).update(status=Transaction.Status.INVENTORY_RESERVE_FAILED)
        
        return Response({
            "transaction_id": transaction_id,
            "error": Transaction.Status.INVENTORY_RESERVE_FAILED,
            "inventory_response": response.json()
            }, status=400)

    Transaction.objects.filter(
        transaction_id=transaction_id
    ).update(status=Transaction.Status.INVENTORY_RESERVED)

    Transaction.objects.filter(
        transaction_id=transaction_id
    ).update(status=Transaction.Status.PAYMENT_PROCESSING)

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
            timeout_status = Transaction.Status.PAYMENT_TIMEOUT
        else:
            timeout_status = Transaction.Status.PAYMENT_CONNECTION_ERROR

        Transaction.objects.filter(
            transaction_id=transaction_id
        ).update(status=timeout_status)

        try:
            payment_check = requests.get(
                "http://127.0.0.1:8001/payments/check/",
                params={
                    "transaction_id": transaction_id,
                },
                timeout=5,
            )
        except (Timeout, ConnectionError):
            return Response({
                "transaction_id": transaction_id,
                "status": timeout_status,
                "error": "Payment outcome could not be determined",
            }, status=503)

        payment_data = payment_check.json()

        if payment_data.get("status") == "CHARGED":
            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status=Transaction.Status.COMPLETED)

            return Response({
                "transaction_id": transaction_id,
                "status": Transaction.Status.COMPLETED,
                "payment_response": payment_data,
            })

        if payment_data.get("status") != "CHARGED":

            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status=Transaction.Status.COMPENSATING)

            try:
                release_response = requests.post(
                    "http://127.0.0.1:8000/products/release/",
                    json={
                        "transaction_id": transaction_id,
                    },
                    timeout=5,
                )
            except (Timeout, ConnectionError) as e:

                Transaction.objects.filter(
                    transaction_id=transaction_id
                ).update(status=Transaction.Status.COMPENSATION_FAILED)

                if isinstance(e, Timeout):
                    cause = Transaction.Status.TIMEOUT
                else:
                    cause = Transaction.Status.CONNECTION_FAILED

                return Response({
                    "transaction_id": transaction_id,
                    "status": Transaction.Status.COMPENSATION_FAILED,
                    "cause": cause,
                    "error": "Could not reach Inventory during compensation",
                }, status=503)

            if release_response.status_code == 200:

                Transaction.objects.filter(
                    transaction_id=transaction_id
                ).update(status=Transaction.Status.COMPENSATED)

                return Response({
                    "transaction_id": transaction_id,
                    "status": Transaction.Status.COMPENSATED,
                    "payment_response": payment_data,
                    "compensation_response": release_response.json(),
                }, status=400)

            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status=Transaction.Status.COMPENSATION_FAILED)

            return Response({
                "transaction_id": transaction_id,
                "status": Transaction.Status.COMPENSATION_FAILED,
                "payment_response": payment_data,
                "compensation_response": release_response.json(),
            }, status=500)
        

    return Response({
        "transaction_id": transaction_id,
        "status": Transaction.Status.COMPLETED,
        "product_id": product_id,
        "quantity": quantity,
        "amount": amount,
        "payment_response": payment_response.json(),
    })

@api_view(["POST"])
def retry_compensation(request, transaction_id):
    transaction = Transaction.objects.get(
        transaction_id=transaction_id
    )

    if transaction.status == Transaction.Status.COMPENSATED:
        return Response({
            "transaction_id": str(transaction_id),
            "status": Transaction.Status.COMPENSATED,
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
                "status": Transaction.Status.RETRY_COMPENSATION_FAILED,
                "cause": "CONNECTION_FAILED",
                "error": "Could not reach Inventory during retry compensation",
            }, status=503)

    if release_response.status_code == 200:
        transaction.status = Transaction.Status.COMPENSATED_ON_RETRY
        transaction.save(update_fields=["status"])
        # Django updates only the status column 
        # instead of saving every field on the Transaction object

        return Response({
            "transaction_id": transaction_id,
            "status": Transaction.Status.COMPENSATED_ON_RETRY,
            "compensation_response": release_response.json(),
        })

    return Response({
        "transaction_id": transaction_id,
        "status": Transaction.Status.RETRY_COMPENSATION_FAILED,
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
