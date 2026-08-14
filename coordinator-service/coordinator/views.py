import uuid
import requests

from rest_framework.decorators import api_view
from rest_framework.response import Response
from requests.exceptions import Timeout, ConnectionError
from django.db import IntegrityError, transaction

from .models import Transaction


@api_view(["POST"])
def create_transaction(request):
    # ── Parse and validate optional client-supplied transaction_id ──
    # Missing → generate uuid4 (original behaviour preserved).
    # Present but invalid → 400.
    # Present and valid → use as-is; idempotency check follows.
    raw_tid = request.data.get("transaction_id")
    if raw_tid is None:
        transaction_id = str(uuid.uuid4())
    else:
        try:
            transaction_id = uuid.UUID(str(raw_tid))
        except (ValueError, AttributeError):
            return Response(
                {"error": "transaction_id must be a valid UUID"},
                status=400,
            )

    product_id = request.data.get("product_id", 1)
    quantity = request.data.get("quantity", 1)
    amount = request.data.get("amount", 500)
    fail_compensation = request.data.get("fail_compensation", False)

    # ── Idempotent create: exactly one attempt, one IntegrityError handler ──
    #
    # transaction.atomic() wraps ONLY this create() call so that the
    # IntegrityError from PostgreSQL's unique constraint is catchable —
    # a failed statement inside a transaction leaves it in an aborted state
    # and requires a rollback before any further DB use. atomic() provides
    # that rollback automatically on exception.
    #
    # The block exits (and PostgreSQL COMMITS) before any outbound HTTP call.
    # No lock is held across the network calls to Inventory or Payment.
    #
    # Race: Thread A and Thread B both attempt create() with the same tid.
    #   Winner (A): INSERT succeeds → atomic() commits → falls through → runs saga.
    #   Loser  (B): INSERT raises IntegrityError → atomic() rolls back →
    #               re-fetches A's row → returns 200 with current status.
    try:
        with transaction.atomic():
            Transaction.objects.create(
                transaction_id=transaction_id,
                status=Transaction.Status.STARTED,
                product_id=product_id,
                quantity=quantity,
                amount=amount,
            )
    except IntegrityError:
        # Duplicate transaction_id — sequential retry or concurrent race loser.
        # Re-fetch the row the winning request created and return its state.
        # The saga must NOT run; that would double-charge and double-reserve.
        existing = Transaction.objects.get(transaction_id=transaction_id)
        return Response(
            {
                "transaction_id": str(existing.transaction_id),
                "status": existing.status,
            },
            status=200,
        )

    # ── atomic() block has exited and committed above this line ──
    # ── The saga starts here. No DB transaction is open. ──

    inventory_fail = request.data.get("coordinator")
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

            release_transaction_id = (
                "fail-release"
                if fail_compensation
                else transaction_id
            )

            Transaction.objects.filter(
                transaction_id=transaction_id
            ).update(status=Transaction.Status.COMPENSATING)

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

    payment_data = payment_response.json()

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

        release_transaction_id = ("fail-release" if fail_compensation else transaction_id)

        Transaction.objects.filter(
            transaction_id=transaction_id
        ).update(status=Transaction.Status.COMPENSATING)

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


@api_view(["GET"])
def transaction_status_update(request, transaction_id):
    transaction = Transaction.objects.filter(
        transaction_id = transaction_id
    )

    transaction.status = Transaction.Status.INVENTORY_RESERVED

    return Response({
        "detail": "status updated"
    })