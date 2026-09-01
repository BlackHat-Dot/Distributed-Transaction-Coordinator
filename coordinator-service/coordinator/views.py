import os
import uuid
import requests

from rest_framework.decorators import api_view
from rest_framework.response import Response
from requests.exceptions import Timeout, ConnectionError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Transaction, SagaEvent
from .saga_log import _set_status_with_event
from .middleware import get_correlation_id


@api_view(["GET"])
def health_check(request):
    return Response({"status": "healthy", "service": "coordinator"})


INVENTORY_BASE_URL = os.getenv("INVENTORY_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
PAYMENT_BASE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")

INVENTORY_RESERVE_URL = f"{INVENTORY_BASE_URL}/seats/reserve/"
INVENTORY_CONFIRM_URL = f"{INVENTORY_BASE_URL}/seats/confirm/"
INVENTORY_RELEASE_URL = f"{INVENTORY_BASE_URL}/seats/release/"
PAYMENT_CHARGE_URL = f"{PAYMENT_BASE_URL}/payments/charge/"
PAYMENT_CHECK_URL = f"{PAYMENT_BASE_URL}/payments/check/"
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "5"))


def _transport_status(exc, timeout_status, conn_status):
    return timeout_status if isinstance(exc, Timeout) else conn_status


def _parse_transaction_id(raw_tid):
    if raw_tid is None:
        return str(uuid.uuid4()), None
    
    try: 
        return str(uuid.UUID(raw_tid)), None
    except (ValueError, AttributeError):
        return None, Response({"error": "transaction_id must be a valid UUID"}, status=400)


def _create_transaction_idempotent(
    transaction_id, seat_ids=None, seat_numbers=None, amount=500, quantity=1, user_id=None, user_email=None, held_by_user_id=None, held_by_user_email=None
):
    uid = user_id or held_by_user_id or "usr_demo_user"
    uemail = user_email or held_by_user_email or "demo@dtc.io"
    try:
        with transaction.atomic():
            Transaction.objects.create(
                transaction_id=transaction_id,
                user_id=uid,
                user_email=uemail,
                status=Transaction.Status.STARTED,
                seat_ids=seat_ids,
                seat_numbers=seat_numbers,
                quantity=quantity,
                amount=amount,
            )
        return True, None
    except IntegrityError:
        existing = Transaction.objects.get(transaction_id=transaction_id)
        return False, Response(
            {
                "transaction_id": str(existing.transaction_id),
                "user_id": existing.user_id,
                "user_email": existing.user_email,
                "held_by_user_id": existing.user_id,
                "held_by_user_email": existing.user_email,
                "status": existing.status,
            },
            status=200,
        )


from coordinator.auth_views import get_actor_from_request


def _reserve_inventory(transaction_id, seat_ids, correlation_id=None, actor_id=None, user_id=None, user_email=None, held_by_user_id=None, held_by_user_email=None):
    uid = user_id or held_by_user_id or actor_id or "usr_demo_user"
    uemail = user_email or held_by_user_email or "demo@dtc.io"
    headers = {"X-Correlation-ID": correlation_id or get_correlation_id() or str(transaction_id)}
    if uid:
        headers["X-User-ID"] = str(uid)
        headers["X-Actor-ID"] = str(uid)
    if uemail:
        headers["X-User-Email"] = str(uemail)
    try:
        response = requests.post(
            INVENTORY_RESERVE_URL,
            json={
                "seat_ids": seat_ids,
                "transaction_id": str(transaction_id),
                "user_id": uid,
                "user_email": uemail,
                "held_by_user_id": uid,
                "held_by_user_email": uemail,
                "actor_id": uid,
            },
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        return response, None
    except (Timeout, ConnectionError) as e:
        status = _transport_status(
            e, Transaction.Status.INVENTORY_TIMEOUT, Transaction.Status.INVENTORY_CONNECTION_ERROR
        )

        _set_status_with_event(
            transaction_id,
            status,
            expected_status=Transaction.Status.STARTED,
            actor=SagaEvent.ACTOR_REQUEST,
            http_status_code=None,
            http_payload=None,
        )
        return None, Response({"transaction_id": str(transaction_id), "status": status}, status=504)


def _confirm_inventory(transaction_id, seat_ids=None, correlation_id=None):
    headers = {"X-Correlation-ID": correlation_id or get_correlation_id() or str(transaction_id)}
    try:
        response = requests.post(
            INVENTORY_CONFIRM_URL,
            json={"seat_ids": seat_ids, "transaction_id": str(transaction_id)},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        return response, None
    except (Timeout, ConnectionError) as e:
        return None, e


def _compensate(transaction_id, fail_compensation, payment_data, payment_status_code=None, correlation_id=None, seat_ids=None):
    release_transaction_id = "fail-release" if fail_compensation else str(transaction_id)
    headers = {"X-Correlation-ID": correlation_id or get_correlation_id() or str(transaction_id)}

    _set_status_with_event(
        transaction_id,
        Transaction.Status.COMPENSATING,
        expected_status=(
            Transaction.Status.PAYMENT_PROCESSING,
            Transaction.Status.PAYMENT_TIMEOUT,
            Transaction.Status.PAYMENT_CONNECTION_ERROR,
        ),
        actor=SagaEvent.ACTOR_REQUEST,
        http_status_code=payment_status_code,
        http_payload=payment_data,
    )

    try:
        release_response = requests.post(
            INVENTORY_RELEASE_URL,
            json={"transaction_id": release_transaction_id, "seat_ids": seat_ids},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except (Timeout, ConnectionError) as e:
        _set_status_with_event(
            transaction_id,
            Transaction.Status.COMPENSATION_FAILED,
            expected_status=Transaction.Status.COMPENSATING,
            actor=SagaEvent.ACTOR_REQUEST,
            http_status_code=None,
            http_payload=None,
        )
        cause = _transport_status(e, Transaction.Status.TIMEOUT, Transaction.Status.CONNECTION_FAILED)
        return Response(
            {
                "transaction_id": str(transaction_id),
                "status": Transaction.Status.COMPENSATION_FAILED,
                "cause": cause,
                "error": "Could not reach Inventory during compensation",
            },
            status=503,
        )

    resp_json = None
    try:
        resp_json = release_response.json()
    except Exception:
        pass

    if release_response.status_code == 200:
        _set_status_with_event(
            transaction_id,
            Transaction.Status.COMPENSATED,
            expected_status=Transaction.Status.COMPENSATING,
            actor=SagaEvent.ACTOR_REQUEST,
            http_status_code=release_response.status_code,
            http_payload=resp_json,
        )
        return Response(
            {
                "transaction_id": str(transaction_id),
                "status": Transaction.Status.COMPENSATED,
                "payment_response": payment_data,
                "compensation_response": resp_json,
            },
            status=400,
        )

    _set_status_with_event(
        transaction_id,
        Transaction.Status.COMPENSATION_FAILED,
        expected_status=Transaction.Status.COMPENSATING,
        actor=SagaEvent.ACTOR_REQUEST,
        http_status_code=release_response.status_code,
        http_payload=resp_json,
    )
    return Response(
        {
            "transaction_id": str(transaction_id),
            "status": Transaction.Status.COMPENSATION_FAILED,
            "payment_response": payment_data,
            "compensation_response": resp_json,
        },
        status=500,
    )


def _finalize_payment_outcome(transaction_id, fail_compensation, payment_data, payment_status_code=200, correlation_id=None, seat_ids=None):
    if payment_data.get("status") == "CHARGED":
        confirm_resp, confirm_err = _confirm_inventory(transaction_id, seat_ids=seat_ids, correlation_id=correlation_id)

        if confirm_resp is not None and confirm_resp.status_code == 200:
            confirm_json = None
            try:
                confirm_json = confirm_resp.json()
            except Exception:
                pass

            _set_status_with_event(
                transaction_id,
                Transaction.Status.COMPLETED,
                expected_status=(
                    Transaction.Status.PAYMENT_PROCESSING,
                    Transaction.Status.PAYMENT_TIMEOUT,
                    Transaction.Status.PAYMENT_CONNECTION_ERROR,
                ),
                actor=SagaEvent.ACTOR_REQUEST,
                http_status_code=payment_status_code,
                http_payload={"payment": payment_data, "inventory": confirm_json},
            )
            return Response({
                "transaction_id": str(transaction_id),
                "status": Transaction.Status.COMPLETED,
                "payment_response": payment_data,
                "inventory_response": confirm_json,
            })

        confirm_json = None
        if confirm_resp is not None:
            try:
                confirm_json = confirm_resp.json()
            except Exception:
                pass

        decline_payload = {
            "error": "Seat hold expired or confirmation failed",
            "payment": payment_data,
            "inventory_confirm": confirm_json or str(confirm_err),
        }
        return _compensate(
            transaction_id,
            fail_compensation,
            decline_payload,
            payment_status_code=confirm_resp.status_code if confirm_resp else 504,
            correlation_id=correlation_id,
            seat_ids=seat_ids,
        )

    return _compensate(
        transaction_id,
        fail_compensation,
        payment_data,
        payment_status_code=payment_status_code,
        correlation_id=correlation_id,
        seat_ids=seat_ids,
    )


def _charge_payment(
    transaction_id,
    amount,
    fail_compensation,
    correlation_id=None,
    seat_ids=None,
    user_id=None,
    user_email=None,
    held_by_user_id=None,
    held_by_user_email=None,
):
    uid = user_id or held_by_user_id or "usr_demo_user"
    uemail = user_email or held_by_user_email or "demo@dtc.io"

    headers = {"X-Correlation-ID": correlation_id or get_correlation_id() or str(transaction_id)}
    if uid:
        headers["X-User-ID"] = str(uid)
        headers["X-Actor-ID"] = str(uid)
    if uemail:
        headers["X-User-Email"] = str(uemail)

    try:
        payment_response = requests.post(
            PAYMENT_CHARGE_URL,
            json={
                "transaction_id": str(transaction_id),
                "amount": amount,
                "user_id": uid,
                "user_email": uemail,
                "held_by_user_id": uid,
                "held_by_user_email": uemail,
            },
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except (Timeout, ConnectionError) as e:
        uncertain_status = _transport_status(
            e, Transaction.Status.PAYMENT_TIMEOUT, Transaction.Status.PAYMENT_CONNECTION_ERROR
        )
    
        _set_status_with_event(
            transaction_id,
            uncertain_status,
            expected_status=Transaction.Status.PAYMENT_PROCESSING,
            actor=SagaEvent.ACTOR_REQUEST,
            http_status_code=None,
            http_payload=None,
        )

        try:
            payment_check = requests.get(
                PAYMENT_CHECK_URL,
                params={"transaction_id": str(transaction_id)},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except (Timeout, ConnectionError):
            return Response(
                {
                    "transaction_id": str(transaction_id),
                    "status": uncertain_status,
                    "error": "Payment outcome could not be determined",
                },
                status=503,
            )

        check_data = None
        try:
            check_data = payment_check.json()
        except Exception:
            pass

        return _finalize_payment_outcome(
            transaction_id,
            fail_compensation,
            check_data or {},
            payment_status_code=payment_check.status_code,
            correlation_id=correlation_id,
            seat_ids=seat_ids,
        )

    resp_data = None
    try:
        resp_data = payment_response.json()
    except Exception:
        pass

    return _finalize_payment_outcome(
        transaction_id,
        fail_compensation,
        resp_data or {},
        payment_status_code=payment_response.status_code,
        correlation_id=correlation_id,
        seat_ids=seat_ids,
    )


def _run_saga_pipeline(
    transaction_id,
    seat_ids=None,
    amount=500,
    fail_compensation=False,
    correlation_id=None,
    actor=SagaEvent.ACTOR_REQUEST,
    actor_id=None,
    user_id=None,
    user_email=None,
    held_by_user_id=None,
    held_by_user_email=None,
):
    correlation_id = correlation_id or str(transaction_id)
    seat_ids = seat_ids or [1]
    uid = user_id or held_by_user_id or actor_id or "usr_demo_user"
    uemail = user_email or held_by_user_email or "demo@dtc.io"

    response, reserve_error = _reserve_inventory(
        transaction_id,
        seat_ids,
        correlation_id=correlation_id,
        actor_id=uid,
        user_id=uid,
        user_email=uemail,
        held_by_user_id=uid,
        held_by_user_email=uemail,
    )
    if reserve_error:
        return reserve_error

    if response.status_code != 200:
        resp_json = None
        try:
            resp_json = response.json()
        except Exception:
            pass

        _set_status_with_event(
            transaction_id,
            Transaction.Status.INVENTORY_RESERVE_FAILED,
            expected_status=Transaction.Status.STARTED,
            actor=actor,
            http_status_code=response.status_code,
            http_payload=resp_json,
        )
        return Response(
            {
                "transaction_id": str(transaction_id),
                "error": Transaction.Status.INVENTORY_RESERVE_FAILED,
                "inventory_response": resp_json,
            },
            status=400,
        )

    resp_json = None
    try:
        resp_json = response.json()
    except Exception:
        pass

    _set_status_with_event(
        transaction_id,
        Transaction.Status.INVENTORY_RESERVED,
        expected_status=Transaction.Status.STARTED,
        actor=actor,
        http_status_code=response.status_code,
        http_payload=resp_json,
    )
    _set_status_with_event(
        transaction_id,
        Transaction.Status.PAYMENT_PROCESSING,
        expected_status=Transaction.Status.INVENTORY_RESERVED,
        actor=actor,
        http_status_code=None,
        http_payload=None,
    )

    return _charge_payment(
        transaction_id,
        amount,
        fail_compensation,
        correlation_id=correlation_id,
        seat_ids=seat_ids,
        user_id=uid,
        user_email=uemail,
        held_by_user_id=uid,
        held_by_user_email=uemail,
    )


def _handle_create_transaction(request):
    transaction_id, error_response = _parse_transaction_id(request.data.get("transaction_id"))
    if error_response:
        return error_response

    seat_ids = request.data.get("seat_ids")
    if not seat_ids and request.data.get("seat_id"):
        seat_ids = [request.data.get("seat_id")]
    if not seat_ids and request.data.get("product_id"):
        seat_ids = [request.data.get("product_id")]
        
    seat_numbers = request.data.get("seat_numbers", "")
    quantity = request.data.get("quantity", len(seat_ids) if seat_ids else 1)
    amount = request.data.get("amount", 500)

    fail_compensation = request.data.get("fail_compensation", False)
    correlation_id = getattr(request, "correlation_id", None) or str(transaction_id)

    actor_info = get_actor_from_request(request)
    user_id = (
        (actor_info.get("user_id") or actor_info.get("actor_id"))
        if actor_info
        else (
            request.data.get("user_id")
            or request.data.get("held_by_user_id")
            or request.data.get("actor_id")
            or request.headers.get("X-User-ID")
            or request.headers.get("X-Actor-ID")
            or "usr_demo_user"
        )
    )
    user_email = (
        (actor_info.get("user_email") or actor_info.get("email"))
        if actor_info
        else (
            request.data.get("user_email")
            or request.data.get("held_by_user_email")
            or request.data.get("email")
            or request.headers.get("X-User-Email")
            or "demo@dtc.io"
        )
    )

    created, dup_response = _create_transaction_idempotent(
        transaction_id=transaction_id,
        seat_ids=seat_ids,
        seat_numbers=seat_numbers,
        amount=amount,
        quantity=quantity,
        user_id=user_id,
        user_email=user_email,
        held_by_user_id=user_id,
        held_by_user_email=user_email,
    )
    if not created:
        return dup_response

    if request.data.get("coordinator"):
        return Response({"detail": "Intentional crash"})

    correlation_id = getattr(request, "correlation_id", None) or str(transaction_id)

    # Try async via Celery (supervisord keeps worker alive alongside gunicorn)
    try:
        from .tasks import execute_saga_task
        execute_saga_task.delay(
            str(transaction_id),
            seat_ids=seat_ids,
            amount=amount,
            fail_compensation=fail_compensation,
            correlation_id=correlation_id,
            actor_id=user_id,
            user_id=user_id,
            user_email=user_email,
            held_by_user_id=user_id,
            held_by_user_email=user_email,
        )
        return Response(
            {
                "transaction_id": str(transaction_id),
                "user_id": user_id,
                "user_email": user_email,
                "held_by_user_id": user_id,
                "held_by_user_email": user_email,
                "status": "PENDING",
                "poll_url": f"/transactions/{transaction_id}/",
                "message": "Saga dispatched to Celery worker",
            },
            status=202,
            headers={"Location": f"/transactions/{transaction_id}/"},
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Celery unavailable (%s) — running synchronously", exc)

    # Fallback: run synchronously if Celery is not available
    return _run_saga_pipeline(
        transaction_id=transaction_id,
        seat_ids=seat_ids,
        amount=amount,
        fail_compensation=fail_compensation,
        correlation_id=correlation_id,
        actor=SagaEvent.ACTOR_REQUEST,
        actor_id=user_id,
        user_id=user_id,
        user_email=user_email,
        held_by_user_id=user_id,
        held_by_user_email=user_email,
    )


def _serialize_transaction(txn):
    terminal_statuses = {
        Transaction.Status.COMPLETED,
        Transaction.Status.COMPENSATED,
        Transaction.Status.COMPENSATION_FAILED,
        Transaction.Status.INVENTORY_RESERVE_FAILED,
        Transaction.Status.COMPENSATED_ON_RETRY,
        Transaction.Status.RETRY_COMPENSATION_FAILED,
        Transaction.Status.TIMEOUT,
        Transaction.Status.PAYMENT_TIMEOUT,
        Transaction.Status.PAYMENT_FAILED,
        Transaction.Status.PAYMENT_CONNECTION_ERROR,
        Transaction.Status.INVENTORY_TIMEOUT,
        Transaction.Status.INVENTORY_CONNECTION_ERROR,
    }
    return {
        "transaction_id": str(txn.transaction_id),
        "user_id": txn.user_id,
        "user_email": txn.user_email,
        "held_by_user_id": txn.user_id,
        "held_by_user_email": txn.user_email,
        "actor_id": txn.user_id,
        "status": txn.status,
        "is_terminal": txn.status in terminal_statuses,
        "seat_ids": txn.seat_ids,
        "seat_numbers": txn.seat_numbers,
        "quantity": txn.quantity,
        "amount": str(txn.amount),
        "created_at": txn.created_at,
        "updated_at": txn.updated_at,
    }


@api_view(["POST"])
def retry_compensation(request, transaction_id):
    txn = Transaction.objects.exclude(
        status=Transaction.Status.COMPLETED
    ).filter(
        transaction_id=transaction_id
    ).first()

    if not txn:
        return Response({
            "detail": "Transaction is already charged and marked as completed"
        })
    
    txn_status_at_entry = txn.status
    
    if txn_status_at_entry == Transaction.Status.COMPENSATED:
        return Response({
            "transaction_id": str(transaction_id),
            "status": Transaction.Status.COMPENSATED,
            "message": "Transaction is already compensated",
        })

    headers = {"X-Correlation-ID": getattr(request, "correlation_id", None) or get_correlation_id() or str(transaction_id)}
    try:
        release_response = requests.post(
            INVENTORY_RELEASE_URL,
            json={"transaction_id": str(transaction_id)},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except (Timeout, ConnectionError) as e:
   
        _set_status_with_event(
            transaction_id,
            to_status=Transaction.Status.RETRY_COMPENSATION_FAILED,
            expected_status=txn_status_at_entry,
            actor=SagaEvent.ACTOR_RETRY,
            http_status_code=None,
            http_payload=None,
        )
        cause = _transport_status(e, "TIMEOUT", "CONNECTION_FAILED")
        return Response(
            {
                "transaction_id": str(transaction_id),
                "status": Transaction.Status.RETRY_COMPENSATION_FAILED,
                "cause": cause,
                "error": "Could not reach Inventory during retry compensation",
            },
            status=503,
        )

    if release_response.status_code == 200:
        transitioned = _set_status_with_event(
            transaction_id,
            to_status=Transaction.Status.COMPENSATED_ON_RETRY,
            expected_status=txn_status_at_entry,
            actor=SagaEvent.ACTOR_RETRY,
            http_status_code=release_response.status_code,
            http_payload=release_response.json(),
        )

        if not transitioned:
            current = Transaction.objects.get(transaction_id=transaction_id)
            return Response({
                "transaction_id": str(transaction_id),
                "status": current.status,
                "message": "Transaction was concurrently resolved by another process",
                "compensation_response": release_response.json(),
            })

        return Response({
            "transaction_id": str(transaction_id),
            "status": Transaction.Status.COMPENSATED_ON_RETRY,
            "compensation_response": release_response.json(),
        })

    _set_status_with_event(
        transaction_id,
        to_status=Transaction.Status.RETRY_COMPENSATION_FAILED,
        expected_status=txn_status_at_entry,
        actor=SagaEvent.ACTOR_RETRY,
        http_status_code=release_response.status_code,
        http_payload=release_response.json(),
    )
    return Response(
        {
            "transaction_id": str(transaction_id),
            "status": Transaction.Status.RETRY_COMPENSATION_FAILED,
            "compensation_response": release_response.json(),
        },
        status=500,
    )


def _serialize_transaction(txn):
    terminal_statuses = {
        Transaction.Status.COMPLETED,
        Transaction.Status.COMPENSATED,
        Transaction.Status.INVENTORY_RESERVE_FAILED,
        Transaction.Status.COMPENSATION_FAILED,
        Transaction.Status.COMPENSATED_ON_RETRY,
        Transaction.Status.RETRY_COMPENSATION_FAILED,
        Transaction.Status.TIMEOUT,
        Transaction.Status.PAYMENT_TIMEOUT,
        Transaction.Status.PAYMENT_FAILED,
        Transaction.Status.PAYMENT_CONNECTION_ERROR,
        Transaction.Status.INVENTORY_TIMEOUT,
        Transaction.Status.INVENTORY_CONNECTION_ERROR,
    }
    return {
        "transaction_id": str(txn.transaction_id),
        "user_id": txn.user_id,
        "user_email": txn.user_email,
        "actor_id": txn.user_id,
        "status": txn.status,
        "is_terminal": txn.status in terminal_statuses,
        "seat_ids": txn.seat_ids,
        "seat_numbers": txn.seat_numbers,
        "quantity": txn.quantity,
        "amount": str(txn.amount),
        "created_at": txn.created_at,
        "updated_at": txn.updated_at,
    }


@api_view(["GET"])
def get_transaction(request, transaction_id):
    try:
        txn = Transaction.objects.get(transaction_id=transaction_id)
    except Transaction.DoesNotExist:
        return Response({"detail": "Transaction not found"}, status=404)
    return Response(_serialize_transaction(txn))


def _handle_list_transactions(request):
    limit_param = request.GET.get("limit", 10)
    offset_param = request.GET.get("offset", 0)
    page_param = request.GET.get("page")
    status_filter = request.GET.get("status")

    try:
        limit = max(1, min(int(limit_param), 100))
    except (ValueError, TypeError):
        limit = 10

    if page_param is not None:
        try:
            page = max(1, int(page_param))
            offset = (page - 1) * limit
        except (ValueError, TypeError):
            offset = 0
    else:
        try:
            offset = max(0, int(offset_param))
        except (ValueError, TypeError):
            offset = 0

    queryset = Transaction.objects.all().order_by("-created_at")

    if status_filter:
        queryset = queryset.filter(status=status_filter)

    total_count = queryset.count()
    txs = queryset[offset : offset + limit]

    data = [_serialize_transaction(t) for t in txs]

    return Response(
        {
            "count": total_count,
            "limit": limit,
            "offset": offset,
            "results": data,
        }
    )


@api_view(["GET", "POST"])
def transactions_endpoint(request):
    if request.method == "GET":
        return _handle_list_transactions(request)
    return _handle_create_transaction(request)


@api_view(["POST"])
def create_transaction(request):
    return _handle_create_transaction(request)


@api_view(["GET"])
def list_transactions(request):
    return _handle_list_transactions(request)


@api_view(["GET"])
def transaction_status_update(request, transaction_id):
    updated = _set_status_with_event(
        transaction_id,
        to_status=Transaction.Status.INVENTORY_RESERVED,
        actor=SagaEvent.ACTOR_REQUEST,
    )
    if not updated:
        return Response({"detail": "transaction not found"}, status=404)
    return Response({"detail": "status updated"})


def _serialize_event(event):
    return {
        "seq": event.seq,
        "from_status": event.from_status,
        "to_status": event.to_status,
        "actor": event.actor,
        "http_status_code": event.http_status_code,
        "http_payload": event.http_payload,
        "created_at": event.created_at,
    }


@api_view(["GET"])
def list_transaction_events(request, transaction_id):
    try:
        txn = Transaction.objects.get(transaction_id=transaction_id)
    except Transaction.DoesNotExist:
        return Response({"detail": "Transaction not found", "events": []}, status=404)
    events = txn.events.order_by("seq")
    return Response({
        "transaction_id": str(txn.transaction_id),
        "status": txn.status,
        "events": [_serialize_event(e) for e in events],
    })


@api_view(["POST"])
def trigger_recovery_sweep(request):
    import io
    from django.core.management import call_command
    out = io.StringIO()
    threshold_seconds = int(request.data.get("threshold_seconds", 0) if request.data else 0)
    call_command("recovery_stuck_transactions", threshold_seconds=threshold_seconds, stdout=out)
    return Response({
        "message": "Recovery sweep complete",
        "output": out.getvalue().strip(),
    })