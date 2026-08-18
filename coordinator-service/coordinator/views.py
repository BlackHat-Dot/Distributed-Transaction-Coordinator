import uuid
import requests

from rest_framework.decorators import api_view
from rest_framework.response import Response
from requests.exceptions import Timeout, ConnectionError
from django.db import IntegrityError, transaction

from .models import Transaction


INVENTORY_RESERVE_URL = "http://127.0.0.1:8000/products/reserve/"
INVENTORY_RELEASE_URL = "http://127.0.0.1:8000/products/release/"
PAYMENT_CHARGE_URL = "http://127.0.0.1:8001/payments/charge/"
PAYMENT_CHECK_URL = "http://127.0.0.1:8001/payments/check/"
REQUEST_TIMEOUT = 5


# helpers

def _set_status(transaction_id, status):
    Transaction.objects.filter(transaction_id=transaction_id).update(status=status)


def _transport_status(exc, timeout_status, conn_status):
    # Map a Timeout/ConnectionError to the right domain status.
    return timeout_status if isinstance(exc, Timeout) else conn_status


def _parse_transaction_id(raw_tid):
    # Missing -> generate uuid4. Present but invalid -> (None, 400 response).
    # Present and valid -> (uuid, None).
    if raw_tid is None:
        return str(uuid.uuid4()), None
    try:
        return str(uuid.UUID(raw_tid)), None # since raw_tid is already str we dont need convertion
    except (ValueError, AttributeError):
        return None, Response({"error": "transaction_id must be a valid UUID"}, status=400)


def _create_transaction_idempotent(transaction_id, product_id, quantity, amount):
    """
        Tries to create the transaction row once.

            => If transaction_id is new: row gets created, returns (True, None).
            => If transaction_id already exists: Postgres raises IntegrityError
               (unique constraint). We catch it, fetch the existing row, and
               return (False, response_with_existing_status) instead.

        Why atomic() is needed here:
            A failed INSERT leaves the DB connection in a broken transaction
            state until it's rolled back. atomic() does that rollback for us
            automatically when IntegrityError is raised, so the .get() call
            in the except block can run cleanly right after.

        Why the `with atomic()` block only wraps create():
            The block covers just the Transaction.objects.create() call, nothing
            else is indented inside it. It does NOT include the later HTTP calls
            to Inventory or Payment. So the DB transaction commits and closes right
            after the row is created, before any network call starts. No DB lock
            is held while we're waiting on another service.

        Returns:
            (True, None) on success
            (False, Response) if transaction_id was a duplicate
        """
    try:
        with transaction.atomic():
            Transaction.objects.create(
                transaction_id=transaction_id,
                status=Transaction.Status.STARTED,
                product_id=product_id,
                quantity=quantity,
                amount=amount,
            )
        return True, None
    except IntegrityError:
        existing = Transaction.objects.get(transaction_id=transaction_id)
        return False, Response(
            {"transaction_id": str(existing.transaction_id), "status": existing.status},
            status=200,
        )


def _reserve_inventory(transaction_id, product_id, quantity):
    # Returns (response, error_response). Exactly one is None.
    try:
        response = requests.post(
            INVENTORY_RESERVE_URL,
            json={"product_id": product_id, "quantity": quantity, "transaction_id": transaction_id},
            timeout=REQUEST_TIMEOUT,
        )
        return response, None
    except (Timeout, ConnectionError) as e:
        status = _transport_status(
            e, Transaction.Status.INVENTORY_TIMEOUT, Transaction.Status.INVENTORY_CONNECTION_ERROR
        )
        _set_status(transaction_id, status)
        return None, Response({"transaction_id": transaction_id, "status": status}, status=504)


def _compensate(transaction_id, fail_compensation, payment_data):
    # Runs the release call and returns the final Response for a
    # non-CHARGED payment outcome
    release_transaction_id = "fail-release" if fail_compensation else transaction_id
    _set_status(transaction_id, Transaction.Status.COMPENSATING)

    try:
        release_response = requests.post(
            INVENTORY_RELEASE_URL,
            json={"transaction_id": release_transaction_id},
            timeout=REQUEST_TIMEOUT,
        )
    except (Timeout, ConnectionError) as e:
        _set_status(transaction_id, Transaction.Status.COMPENSATION_FAILED)
        cause = _transport_status(e, Transaction.Status.TIMEOUT, Transaction.Status.CONNECTION_FAILED)
        return Response(
            {
                "transaction_id": transaction_id,
                "status": Transaction.Status.COMPENSATION_FAILED,
                "cause": cause,
                "error": "Could not reach Inventory during compensation",
            },
            status=503,
        )

    if release_response.status_code == 200:
        _set_status(transaction_id, Transaction.Status.COMPENSATED)
        return Response(
            {
                "transaction_id": transaction_id,
                "status": Transaction.Status.COMPENSATED,
                "payment_response": payment_data,
                "compensation_response": release_response.json(),
            },
            status=400,
        )

    _set_status(transaction_id, Transaction.Status.COMPENSATION_FAILED)
    return Response(
        {
            "transaction_id": transaction_id,
            "status": Transaction.Status.COMPENSATION_FAILED,
            "payment_response": payment_data,
            "compensation_response": release_response.json(),
        },
        status=500,
    )


def _finalize_payment_outcome(transaction_id, fail_compensation, payment_data):
    # Shared by the direct-charge-response path and the
    # uncertain-outcome/check path: decide COMPLETED vs compensate
    if payment_data.get("status") == "CHARGED":
        _set_status(transaction_id, Transaction.Status.COMPLETED)
        return Response({
            "transaction_id": transaction_id,
            "status": Transaction.Status.COMPLETED,
            "payment_response": payment_data,
        })
    return _compensate(transaction_id, fail_compensation, payment_data)


def _charge_payment(transaction_id, amount, fail_compensation):
    """
    Normal case: POST to /payments/charge/ succeeds and returns a status
    (CHARGED or not) right away, that's the answer done.

    Timeout/connection-error case: we don't know if the charge actually
    went through on Payment's side or not. So we call GET /payments/check/
    to look up the real outcome instead of guessing. Once we have a real
    status from either path, the same COMPLETED-vs-compensate decision
    is made.
    """
    try:
        payment_response = requests.post(
            PAYMENT_CHARGE_URL,
            json={"transaction_id": transaction_id, "amount": amount},
            timeout=REQUEST_TIMEOUT,
        )
    except (Timeout, ConnectionError) as e:
        uncertain_status = _transport_status(
            e, Transaction.Status.PAYMENT_TIMEOUT, Transaction.Status.PAYMENT_CONNECTION_ERROR
        )
        _set_status(transaction_id, uncertain_status)

        try:
            payment_check = requests.get(
                PAYMENT_CHECK_URL, params={"transaction_id": transaction_id}, timeout=REQUEST_TIMEOUT
            )
        except (Timeout, ConnectionError):
            return Response(
                {
                    "transaction_id": transaction_id,
                    "status": uncertain_status,
                    "error": "Payment outcome could not be determined",
                },
                status=503,
            )

        return _finalize_payment_outcome(transaction_id, fail_compensation, payment_check.json())

    return _finalize_payment_outcome(transaction_id, fail_compensation, payment_response.json())


# views 

@api_view(["POST"])
def create_transaction(request):
    transaction_id, error_response = _parse_transaction_id(request.data.get("transaction_id"))
    if error_response:
        return error_response

    product_id = request.data.get("product_id", 1)
    quantity = request.data.get("quantity", 1)
    amount = request.data.get("amount", 500)
    fail_compensation = request.data.get("fail_compensation", False)

    created, dup_response = _create_transaction_idempotent(transaction_id, product_id, quantity, amount)
    if not created:
        return dup_response

    # atomic() block above has already committed. Saga starts here;
    # no DB transaction is open across the outbound HTTP calls, and that is why
    # the block is small

    response, reserve_error = _reserve_inventory(transaction_id, product_id, quantity)
    if reserve_error:
        return reserve_error

    if request.data.get("coordinator"):
        # Test hook: force an early return after a successful reserve
        # call, without inspecting its result to simulate a coordinator
        # crash mid-saga.
        return Response({"detail": "Intentional crash"})

    if response.status_code != 200:
        _set_status(transaction_id, Transaction.Status.INVENTORY_RESERVE_FAILED)
        return Response(
            {
                "transaction_id": transaction_id,
                "error": Transaction.Status.INVENTORY_RESERVE_FAILED,
                "inventory_response": response.json(),
            },
            status=400,
        )

    _set_status(transaction_id, Transaction.Status.PAYMENT_PROCESSING)

    return _charge_payment(transaction_id, amount, fail_compensation)


@api_view(["POST"])
def retry_compensation(request, transaction_id):
    txn = Transaction.objects.get(transaction_id=transaction_id)

    if txn.status == Transaction.Status.COMPENSATED:
        return Response({
            "transaction_id": str(transaction_id),
            "status": Transaction.Status.COMPENSATED,
            "message": "Transaction is already compensated",
        })

    try:
        release_response = requests.post(
            INVENTORY_RELEASE_URL,
            json={"transaction_id": str(transaction_id)},
            timeout=REQUEST_TIMEOUT,
        )
    except (Timeout, ConnectionError) as e:
        _set_status(transaction_id, Transaction.Status.RETRY_COMPENSATION_FAILED)
        cause = _transport_status(e, "TIMEOUT", "CONNECTION_FAILED")
        return Response(
            {
                "transaction_id": transaction_id,
                "status": Transaction.Status.RETRY_COMPENSATION_FAILED,
                "cause": cause,
                "error": "Could not reach Inventory during retry compensation",
            },
            status=503,
        )

    if release_response.status_code == 200:
        txn.status = Transaction.Status.COMPENSATED_ON_RETRY
        txn.save(update_fields=["status"])
        return Response({
            "transaction_id": transaction_id,
            "status": Transaction.Status.COMPENSATED_ON_RETRY,
            "compensation_response": release_response.json(),
        })

    return Response(
        {
            "transaction_id": transaction_id,
            "status": Transaction.Status.RETRY_COMPENSATION_FAILED,
            "compensation_response": release_response.json(),
        },
        status=500,
    )


def _serialize_transaction(txn):
    return {
        "transaction_id": str(txn.transaction_id),
        "status": txn.status,
        "product_id": txn.product_id,
        "quantity": txn.quantity,
        "amount": str(txn.amount),
        "created_at": txn.created_at,
        "updated_at": txn.updated_at,
    }


@api_view(["GET"])
def get_transaction(request, transaction_id):
    txn = Transaction.objects.get(transaction_id=transaction_id)
    return Response(_serialize_transaction(txn))


@api_view(["GET"])
def list_transactions(request):
    transactions = Transaction.objects.all().order_by("-created_at")
    return Response([_serialize_transaction(txn) for txn in transactions])


@api_view(["GET"])
def transaction_status_update(request, transaction_id):
    updated = Transaction.objects.filter(transaction_id=transaction_id).update(
        status=Transaction.Status.INVENTORY_RESERVED
    )
    if not updated:
        return Response({"detail": "transaction not found"}, status=404)
    return Response({"detail": "status updated"})