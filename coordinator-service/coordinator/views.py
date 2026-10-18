import hmac
import json
import logging
import os
import time
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
import redis
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, Timeout
from rest_framework.decorators import api_view
from rest_framework.response import Response

from coordinator.auth_views import get_user_from_request
from .middleware import get_correlation_id
from .models import SagaEvent, Transaction
from .recovery import execute_compensation_sweep
from .saga_log import _set_status_with_event

logger = logging.getLogger(__name__)

# Outbound HTTP connection timeout (seconds)
REQUEST_CONNECT_TIMEOUT = float(os.getenv("REQUEST_CONNECT_TIMEOUT", "2.0"))
# Outbound HTTP read timeout (seconds, falling back to REQUEST_TIMEOUT env)
REQUEST_READ_TIMEOUT = float(os.getenv("REQUEST_READ_TIMEOUT", os.getenv("REQUEST_TIMEOUT", "5.0")))
# Request timeout tuple (connect, read) for outbound HTTP calls
REQUEST_TIMEOUT = (REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT)

# Downstream Inventory microservice base URL
INVENTORY_BASE_URL = os.getenv("INVENTORY_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
# Downstream Payment microservice base URL
PAYMENT_BASE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")

# Downstream Inventory microservice endpoints
INVENTORY_RESERVE_URL = f"{INVENTORY_BASE_URL}/seats/reserve/"
INVENTORY_CONFIRM_URL = f"{INVENTORY_BASE_URL}/seats/confirm/"
INVENTORY_RELEASE_URL = f"{INVENTORY_BASE_URL}/seats/release/"
INVENTORY_PRICING_URL = f"{INVENTORY_BASE_URL}/seats/pricing/"

# Downstream Payment microservice endpoints
PAYMENT_CHARGE_URL = f"{PAYMENT_BASE_URL}/payments/charge/"
PAYMENT_CHECK_URL = f"{PAYMENT_BASE_URL}/payments/check/"
PAYMENT_REFUND_URL = f"{PAYMENT_BASE_URL}/payments/refund/"
PAYMENT_VOID_URL = f"{PAYMENT_BASE_URL}/payments/void/"

# Shared secret for service-to-service internal authentication (X-Internal-Service-Key)
INTERNAL_SERVICE_KEY = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY")

def _corr_id(correlation_id=None):
    """Return active correlation ID, middleware ContextVar, or fresh UUIDv4."""
    return correlation_id or get_correlation_id() or str(uuid.uuid4())

# i know
def _verify_staff_key(header_val):
    """Constant-time verification of X-Staff-Key header."""
    if not header_val or not isinstance(header_val, str) or not header_val.strip():
        return False
    staff_key = os.getenv("STAFF_API_KEY", "").strip()
    if not staff_key:
        return False
    return hmac.compare_digest(header_val.strip().encode("utf-8"), staff_key.encode("utf-8"))


def _faults_on():
    """Return True only if DEBUG=True and ENABLE_FAULT_INJECTION is enabled."""
    if not getattr(settings, "DEBUG", False):
        return False
    flag = getattr(settings, "ENABLE_FAULT_INJECTION", False)
    return bool(flag)


_HTTP_SESSION = None


def _get_http_session():
    """Return a pooled requests.Session instance."""
    global _HTTP_SESSION
    if _HTTP_SESSION is None:
        _HTTP_SESSION = requests.Session()
        adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25)
        _HTTP_SESSION.mount("http://", adapter)
        _HTTP_SESSION.mount("https://", adapter)
    return _HTTP_SESSION


def _http_post(url, **kwargs):
    """Execute outbound HTTP POST request."""
    timeout = kwargs.pop("timeout", REQUEST_TIMEOUT)
    if getattr(settings, "IS_TESTING", False):
        return requests.post(url, timeout=timeout, **kwargs)
    return _get_http_session().post(url, timeout=timeout, **kwargs)


def _http_get(url, **kwargs):
    """Execute outbound HTTP GET request."""
    timeout = kwargs.pop("timeout", REQUEST_TIMEOUT)
    if getattr(settings, "IS_TESTING", False):
        return requests.get(url, timeout=timeout, **kwargs)
    return _get_http_session().get(url, timeout=timeout, **kwargs)


def _release_inventory(tx_id, seat_ids=None, correlation_id=None, reason=""):
    """Best-effort release of seat holds to Inventory Service."""
    headers = {
        "X-Correlation-ID": _corr_id(correlation_id),
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
        "X-Idempotency-Key": str(tx_id),
    }
    payload = {"transaction_id": str(tx_id)}
    if seat_ids is not None:
        payload["seat_ids"] = seat_ids
    try:
        return _http_post(
            INVENTORY_RELEASE_URL,
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except RequestException as exc:
        logger.warning(
            "Failed to release hold for tx %s %s: %s [correlation_id=%s]",
            tx_id,
            reason,
            exc,
            headers["X-Correlation-ID"],
        )
        return None


# =============================================================================
# PUBLIC API ENDPOINTS
# =============================================================================

# i know
@api_view(["GET"])
def health_check(request):
    """Liveness probe returning healthy service status."""
    return Response({"status": "healthy", "service": "coordinator"})


@api_view(["GET", "POST"])
def transactions_endpoint(request):
    """Route GET to list transactions and POST to create or pay transaction."""
    if request.method == "GET":
        return _list_txns(request)
    return _create_txn(request)


@api_view(["GET"])
def list_transactions(request):
    """Public GET endpoint to list transactions with filtering and pagination."""
    return _list_txns(request)


def _can_access(txn, user_id, is_staff=False):
    """Check if caller has permission to view or modify transaction."""
    if is_staff:
        return True
    return bool(txn and txn.user_id and user_id and str(txn.user_id) == str(user_id))


@api_view(["GET"])
def get_transaction(request, transaction_id):
    """Retrieve single transaction details with ownership check."""
    is_authenticated, auth_user_id, is_staff = _get_caller(request)
    if not is_authenticated:
        return Response({"error": "Authentication required."}, status=401)

    try:
        txn = Transaction.objects.get(transaction_id=transaction_id)
    except Transaction.DoesNotExist:
        return Response({"detail": "Transaction not found"}, status=404)

    if not _can_access(txn, auth_user_id, is_staff):
        return Response({"error": "You do not have permission to view this transaction."}, status=403)

    return Response(_txn_json(txn))


@api_view(["GET"])
def list_transaction_events(request, transaction_id):
    """List saga audit events for transaction in chronological order."""
    is_authenticated, auth_user_id, is_staff = _get_caller(request)
    if not is_authenticated:
        return Response({"error": "Authentication required."}, status=401)

    try:
        txn = Transaction.objects.get(transaction_id=transaction_id)
    except Transaction.DoesNotExist:
        return Response({"detail": "Transaction not found", "events": []}, status=404)

    if not _can_access(txn, auth_user_id, is_staff):
        return Response({"error": "You do not have permission to view this transaction's events."}, status=403)

    events = txn.events.order_by("seq")
    return Response({
        "transaction_id": str(txn.transaction_id),
        "status": txn.status,
        "events": [_event_json(event) for event in events],
    })


def transaction_stream(request, transaction_id):
    """Server-Sent Events endpoint streaming transaction transitions via Redis pubsub."""
    if request.method == "OPTIONS":
        return HttpResponse(status=204)

    is_authenticated, auth_user_id, is_staff = _get_caller(request)
    if not is_authenticated:
        return JsonResponse({"error": "Authentication required."}, status=401)

    try:
        txn = Transaction.objects.get(transaction_id=transaction_id)
    except Transaction.DoesNotExist:
        return JsonResponse({"detail": "Transaction not found"}, status=404)

    if not _can_access(txn, auth_user_id, is_staff):
        return JsonResponse({"error": "You do not have permission to view this transaction."}, status=403)

    terminal_statuses = Transaction.TERMINAL_STATUSES

    if txn.status in terminal_statuses:
        def terminal_generator():
            try:
                initial_msg = json.dumps({
                    "transaction_id": str(txn.transaction_id),
                    "status": txn.status,
                    "is_terminal": True,
                    "needs_recovery": txn.status in Transaction.RECOVERABLE_STATUSES,
                })
                yield f"data: {initial_msg}\n\n"
            finally:
                try:
                    from django.db import close_old_connections
                    close_old_connections()
                except Exception:
                    pass
        return StreamingHttpResponse(terminal_generator(), content_type="text/event-stream")

    def event_generator():
        channel_name = f"dtc:tx_stream:{transaction_id}"
        broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")

        redis_client = None
        pubsub = None
        try:
            redis_client = redis.Redis.from_url(
                broker_url,
                decode_responses=True,
                socket_connect_timeout=2.0,
            )
            pubsub = redis_client.pubsub()
            pubsub.subscribe(channel_name)

            # Emit current state immediately to connected client
            current_txn = Transaction.objects.filter(transaction_id=transaction_id).first()
            if current_txn:
                is_term = current_txn.status in terminal_statuses
                initial_msg = json.dumps({
                    "transaction_id": str(current_txn.transaction_id),
                    "status": current_txn.status,
                    "is_terminal": is_term,
                    "needs_recovery": current_txn.status in Transaction.RECOVERABLE_STATUSES,
                })
                yield f"data: {initial_msg}\n\n"
                if is_term:
                    return

            start_time = time.monotonic()
            max_duration = 30.0
            last_db_check = start_time

            while True:
                now = time.monotonic()
                if now - start_time > max_duration:
                    timeout_msg = json.dumps({
                        "transaction_id": str(transaction_id),
                        "status": "STREAM_TIMEOUT",
                        "is_terminal": False,
                        "needs_recovery": False,
                    })
                    yield f"data: {timeout_msg}\n\n"
                    break

                msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg.get("type") == "message":
                    raw_data = msg.get("data")
                    yield f"data: {raw_data}\n\n"
                    try:
                        parsed = json.loads(raw_data)
                        if parsed.get("is_terminal"):
                            break
                    except Exception:
                        pass
                else:
                    yield ": ping\n\n"

                    # Periodic DB check in case Redis message was dropped
                    if now - last_db_check >= 3.0:
                        last_db_check = now
                        db_txn = Transaction.objects.filter(transaction_id=transaction_id).first()
                        if db_txn and db_txn.status in terminal_statuses:
                            term_msg = json.dumps({
                                "transaction_id": str(db_txn.transaction_id),
                                "status": db_txn.status,
                                "is_terminal": True,
                                "needs_recovery": db_txn.status in Transaction.RECOVERABLE_STATUSES,
                            })
                            yield f"data: {term_msg}\n\n"
                            break


        except Exception as exc:
            logger.warning("SSE stream error for tx %s: %s", transaction_id, exc)
            err_msg = json.dumps({
                "transaction_id": str(transaction_id),
                "status": "STREAM_ERROR",
                "is_terminal": False,
                "error": str(exc),
            })
            yield f"data: {err_msg}\n\n"
        finally:
            if pubsub:
                try:
                    pubsub.unsubscribe(channel_name)
                    pubsub.close()
                except Exception:
                    pass
            if redis_client:
                try:
                    redis_client.close()
                except Exception:
                    pass
            try:
                from django.db import close_old_connections
                close_old_connections()
            except Exception:
                pass

    response = StreamingHttpResponse(event_generator(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    response["Connection"] = "keep-alive"
    return response


ALLOWED_RETRY_STATUSES = (
    Transaction.Status.COMPENSATION_FAILED,
    Transaction.Status.RETRY_COMPENSATION_FAILED,
    Transaction.Status.PAYMENT_TIMEOUT,
    Transaction.Status.PAYMENT_CONNECTION_ERROR,
    Transaction.Status.INVENTORY_TIMEOUT,
    Transaction.Status.INVENTORY_CONNECTION_ERROR,
)


@api_view(["POST"])
def retry_compensation(request, transaction_id):
    """Staff-only endpoint to re-trigger failed compensation for stuck transactions."""
    is_authenticated, user_id, is_staff = _get_caller(request)
    if not is_authenticated:
        return Response({"error": "Authentication required"}, status=401)
    if not is_staff:
        return Response(
            {"error": "You do not have permission to retry compensation for this transaction."},
            status=403,
        )

    txn = Transaction.objects.filter(transaction_id=transaction_id).first()
    if not txn:
        return Response({"error": "Transaction not found"}, status=404)

    if txn.status in (Transaction.Status.COMPENSATED, Transaction.Status.COMPENSATED_ON_RETRY):
        return Response({
            "transaction_id": str(transaction_id),
            "status": txn.status,
            "message": "Transaction is already compensated",
        }, status=200)

    if txn.status not in ALLOWED_RETRY_STATUSES:
        return Response(
            {
                "transaction_id": str(transaction_id),
                "status": txn.status,
                "error": f"Transaction in status '{txn.status}' is not eligible for retry compensation",
            },
            status=400,
        )

    # CAS-claim to RECOVERY_IN_PROGRESS before invoking sweep
    claimed = _set_status_with_event(
        transaction_id,
        to_status=Transaction.Status.RECOVERY_IN_PROGRESS,
        expected_status=txn.status,
        actor=SagaEvent.ACTOR_RETRY,
        http_status_code=None,
        http_payload=None,
    )
    if not claimed:
        current = Transaction.objects.filter(transaction_id=transaction_id).first()
        curr_status = current.status if current else "UNKNOWN"
        return Response({
            "transaction_id": str(transaction_id),
            "status": curr_status,
            "message": "Transaction was concurrently resolved by another process",
        }, status=409)

    correlation_id = _corr_id(getattr(request, "correlation_id", None))
    try:
        outcome = execute_compensation_sweep(
            transaction_id=transaction_id,
            actor=SagaEvent.ACTOR_RETRY,
            original_status=txn.status,
            correlation_id=correlation_id,
            get_func=_http_get,
            post_func=_http_post,
        )
    except Exception as exc:
        logger.error(
            "execute_compensation_sweep raised unexpectedly in retry_compensation for tx %s: %s; reverting claim",
            transaction_id,
            exc,
            exc_info=True,
        )
        _set_status_with_event(
            transaction_id=transaction_id,
            to_status=txn.status,
            expected_status=Transaction.Status.RECOVERY_IN_PROGRESS,
            actor=SagaEvent.ACTOR_RETRY,
            http_status_code=None,
            http_payload={"error": f"Internal error during compensation retry: {str(exc)}"},
        )
        return Response(
            {
                "transaction_id": str(transaction_id),
                "status": txn.status,
                "error": "Compensation retry encountered an unexpected error; claim released for retry",
            },
            status=500,
        )

    response_data = {
        "transaction_id": str(transaction_id),
        "status": outcome["status"],
    }
    if outcome.get("compensation_response"):
        response_data["compensation_response"] = outcome["compensation_response"]
    if outcome.get("refund_response"):
        response_data["refund_response"] = outcome["refund_response"]
    if "error" in outcome:
        response_data["error"] = outcome["error"]

    return Response(response_data, status=outcome["status_code"])


@api_view(["POST"])
def trigger_recovery_sweep(request):
    """Staff endpoint to execute an immediate on-demand crash-recovery sweep."""
    import io
    from django.core.management import call_command

    is_authenticated, auth_user_id, is_staff = _get_caller(request)
    if not is_authenticated:
        return Response({"error": "Authentication required to trigger recovery sweep."}, status=401)
    if not is_staff:
        return Response({"error": "Staff privileges required to trigger recovery sweep."}, status=403)

    out = io.StringIO()
    raw_threshold = 60
    if request.data and "threshold_seconds" in request.data:
        val = request.data["threshold_seconds"]
        if isinstance(val, bool) or not isinstance(val, (int, str)):
            return Response(
                {"error": "threshold_seconds must be a valid integer.", "code": "INVALID_THRESHOLD_SECONDS"},
                status=400,
            )
        try:
            raw_threshold = int(val)
            if raw_threshold < 0:
                return Response(
                    {"error": "threshold_seconds must be a non-negative integer.", "code": "INVALID_THRESHOLD_SECONDS"},
                    status=400,
                )
        except (ValueError, TypeError):
            return Response(
                {"error": "threshold_seconds must be a valid integer.", "code": "INVALID_THRESHOLD_SECONDS"},
                status=400,
            )

    min_floor = 5 if getattr(settings, "DEBUG", False) else int(os.getenv("RECOVERY_MIN_THRESHOLD_SECONDS", "30"))
    threshold_seconds = max(raw_threshold, min_floor)

    from coordinator.tasks import get_redis_client, LOCK_KEY, RELEASE_LOCK_SCRIPT
    redis_client = None
    lock_token = None
    lock_acquired = False
    try:
        redis_client = get_redis_client()
        lock_token = str(uuid.uuid4())
        lock_acquired = bool(redis_client.set(LOCK_KEY, lock_token, nx=True, ex=120))
        if not lock_acquired:
            return Response(
                {"error": "Recovery sweep is already in progress.", "code": "RECOVERY_ALREADY_IN_PROGRESS"},
                status=409,
            )
    except (redis.ConnectionError, redis.TimeoutError, ConnectionRefusedError, OSError) as exc:
        logger.warning("Redis unavailable for recovery lock check: %s", exc)
        redis_client = None
    except Exception as exc:
        logger.warning("Redis lock check failed in trigger_recovery_sweep: %s", exc)

    try:
        call_command("recovery_stuck_transactions", threshold_seconds=threshold_seconds, stdout=out)
        if redis_client:
            try:
                redis_client.set("dtc:recovery:last_run_timestamp", str(time.time()))
            except Exception:
                pass
    finally:
        if redis_client and lock_acquired and lock_token:
            try:
                redis_client.eval(RELEASE_LOCK_SCRIPT, 1, LOCK_KEY, lock_token)
            except Exception as exc:
                logger.error("Failed to release lock '%s': %s", LOCK_KEY, exc)

    return Response({
        "message": "Recovery sweep complete",
        "threshold_seconds_applied": threshold_seconds,
        "output": out.getvalue().strip(),
    })


@api_view(["GET"])
def recovery_status(request):
    """Staff endpoint returning recovery sweeper timing and health metrics."""
    is_authenticated, auth_user_id, is_staff = _get_caller(request)
    if not is_authenticated:
        return Response({"error": "Authentication required to view recovery status."}, status=401)
    if not is_staff:
        return Response({"error": "Staff privileges required to view recovery status."}, status=403)

    from coordinator.tasks import get_redis_client

    last_run_ts = None
    redis_available = False
    try:
        client = get_redis_client()
        val = client.get("dtc:recovery:last_run_timestamp")
        if val:
            last_run_ts = float(val.decode("utf-8") if isinstance(val, bytes) else val)
        redis_available = True
    except Exception:
        pass

    now = time.time()
    cycle = 90.0
    if last_run_ts:
        elapsed = now - last_run_ts
        next_in = max(1, int(cycle - (elapsed % cycle)))
    else:
        next_in = max(1, int(cycle - (now % cycle)))

    return Response({
        "status": "active",
        "service": "celery_beat",
        "schedule_seconds": int(cycle),
        "last_run_timestamp": last_run_ts,
        "seconds_until_next_run": next_in,
        "redis_connected": redis_available,
        "server_time": now,
    }, status=200)


# =============================================================================
# CONTROLLER REQUEST HANDLERS
# =============================================================================

def _get_prices(seat_ids=None, correlation_id=None):
    """Query Inventory service for seat pricing and readable seat labels."""
    headers = {
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
        "X-Correlation-ID": _corr_id(correlation_id),
    }

    seats_list = None
    try:
        resp = _http_post(
            INVENTORY_PRICING_URL,
            json={"seat_ids": seat_ids},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            seats_list = resp.json()
    except Exception as exc:
        return None, None, None, Response(
            {
                "error": "Could not connect to Inventory Service to verify seat pricing.",
                "code": "INVENTORY_UNAVAILABLE",
                "detail": str(exc),
            },
            status=503,
        )

    if seats_list and isinstance(seats_list, list):
        if len(seats_list) == len(seat_ids):
            total_amount = sum(Decimal(str(seat.get("price"))) for seat in seats_list)
            resolved_seat_ids = [seat["id"] for seat in seats_list]
            resolved_seat_numbers = ", ".join(f"{seat.get('row', '')}{seat.get('seat_number', '')}" for seat in seats_list)
            return total_amount, resolved_seat_ids, resolved_seat_numbers, None
        return None, None, None, Response(
            {
                "error": "One or more requested seats could not be found in inventory.",
                "code": "SEATS_NOT_FOUND",
            },
            status=404,
        )

    return None, None, None, Response(
        {
            "error": "Could not retrieve seat catalog from Inventory Service.",
            "code": "INVENTORY_UNAVAILABLE",
        },
        status=503,
    )


def _validate_seats(seats):
    """Validate seat_ids list: integer types, no duplicates, max seat limit."""
    if not isinstance(seats, list) or any(type(s) is not int for s in seats):
        return Response({"error": "seat_ids must be a list of integers.", "code": "INVALID_SEAT_IDS"}, status=400)
    if len(seats) != len(set(seats)):
        return Response({"error": "Duplicate seat_ids are not allowed.", "code": "DUPLICATE_SEATS"}, status=400)
    max_seats = int(os.getenv("MAX_SEATS_PER_TRANSACTION", "5"))
    if len(seats) > max_seats:
        return Response(
            {"error": f"Cannot reserve more than {max_seats} seats in a single transaction.", "code": "EXCEEDED_MAX_SEATS"},
            status=400,
        )
    return None


def _check_body(request):
    """Validate incoming payload structure for hold, pay, or one-shot saga."""
    if not isinstance(request.data, dict):
        return None, None, None, Response(
            {"error": "Request body must be a valid JSON object"}, status=400
        )

    action = request.data.get("action") or None
    if action not in (None, "hold", "pay"):
        return None, None, None, Response(
            {
                "error": "Invalid action. Supported values are 'hold' or 'pay'.",
                "code": "INVALID_ACTION",
            },
            status=400,
        )

    allowed_by_action = {
        "pay": {"action", "transaction_id", "seat_ids"},
        "hold": {"action", "seat_ids"},
    }
    allowed_fields = allowed_by_action.get(action, {"seat_ids"})

    if _faults_on():
        allowed_fields = allowed_fields | {"fail_compensation", "coordinator", "fail_at_step"}

    invalid_fields = [k for k in request.data if k not in allowed_fields]
    if invalid_fields:
        return None, None, None, Response(
            {
                "error": f"Invalid field(s) sent in payload: {', '.join(invalid_fields)}. These fields cannot be supplied by the client.",
                "code": "INVALID_FIELDS",
                "invalid_fields": invalid_fields,
            },
            status=400,
        )

    if action == "pay":
        raw_tid = request.data.get("transaction_id")
        if not raw_tid:
            return None, None, None, Response(
                {"error": "transaction_id is required for action='pay'.", "code": "TRANSACTION_ID_REQUIRED"},
                status=400,
            )
        try:
            pay_tid = uuid.UUID(str(raw_tid))
        except (ValueError, TypeError):
            return None, None, None, Response(
                {"error": "transaction_id must be a valid UUID.", "code": "INVALID_TRANSACTION_ID"},
                status=400,
            )
        pay_seats = request.data.get("seat_ids")
        if pay_seats is not None:
            seat_err = _validate_seats(pay_seats)
            if seat_err:
                return None, None, None, seat_err
        return None, action, pay_tid, None

    raw_seats = request.data.get("seat_ids")
    if not raw_seats or not isinstance(raw_seats, list):
        return None, None, None, Response(
            {"error": "seat_ids is required and must be a non-empty list of integers.", "code": "SEATS_REQUIRED"},
            status=400,
        )
    seat_err = _validate_seats(raw_seats)
    if seat_err:
        return None, None, None, seat_err

    return list(raw_seats), action, None, None


def _create_txn(request):
    """Handle POST request to create hold, pay hold, or run full one-shot saga."""
    seat_ids, action, pay_tid, invalid = _check_body(request)
    if invalid:
        return invalid

    is_testing = bool(getattr(settings, "IS_TESTING", False))
    is_dev = getattr(settings, "DEBUG", False)

    ident = _get_caller(request)
    if not ident[0]:
        return Response(
            {
                "error": "Authentication required. Your session has expired or you are not signed in. Please sign in to book seats.",
                "code": "AUTHENTICATION_REQUIRED",
            },
            status=401,
        )

    user_id = ident[1]
    is_staff = ident[2]
    user_email = getattr(ident, "user_email", None) or (ident[3] if len(ident) > 3 else None)

    correlation_id = request.correlation_id
    if not correlation_id:
        raise RuntimeError("CorrelationIdMiddleware is missing or misconfigured.")

    fail_compensation = False
    if _faults_on() and request.data:
        fail_compensation = bool(request.data.get("fail_compensation", False))

    enable_test_payment_failure = bool(getattr(settings, "ENABLE_TEST_PAYMENT_FAILURE", False))
    simulate_decline = False
    if enable_test_payment_failure:
        simulate_decline = (
            request.query_params.get("simulate_payment_decline", "").lower() in ("true", "1", "yes", "t")
            or request.headers.get("X-Simulate-Payment-Decline", "").lower() in ("true", "1", "yes", "t")
        )

    is_sync = request.query_params.get("sync") == "true"

    # Phase 1: HOLD
    if action == "hold":
        tx_id = str(uuid.uuid4())
        amount, seat_ids, seat_numbers, price_error = _get_prices(
            seat_ids=seat_ids,
            correlation_id=correlation_id,
        )
        if price_error:
            return price_error

        quantity = len(seat_ids)
        created, dup_response = _create_transaction(
            transaction_id=tx_id,
            seat_ids=seat_ids,
            seat_numbers=seat_numbers,
            amount=amount,
            quantity=quantity,
            user_id=user_id,
            user_email=user_email,
        )
        if not created:
            return dup_response

        response, reserve_error = _reserve(
            tx_id,
            seat_ids,
            correlation_id=correlation_id,
            user_id=user_id,
            user_email=user_email,
        )
        if reserve_error:
            _release_inventory(tx_id, seat_ids, correlation_id, "on reserve failure")
            _set_status_with_event(
                tx_id,
                Transaction.Status.INVENTORY_RESERVE_FAILED,
                expected_status=[
                    Transaction.Status.STARTED,
                    Transaction.Status.INVENTORY_TIMEOUT,
                    Transaction.Status.INVENTORY_CONNECTION_ERROR,
                ],
                actor=SagaEvent.ACTOR_REQUEST,
            )
            return reserve_error

        body = _parse_json_response(response)
        if response.status_code != 200:
            _release_inventory(tx_id, seat_ids, correlation_id, "on reserve non-200")
            _set_status_with_event(
                tx_id,
                Transaction.Status.INVENTORY_RESERVE_FAILED,
                expected_status=Transaction.Status.STARTED,
                actor=SagaEvent.ACTOR_REQUEST,
                http_status_code=response.status_code,
                http_payload=body,
            )
            return Response(
                body or {
                    "error": "Inventory reserve failed",
                    "code": "INVENTORY_RESERVE_FAILED",
                },
                status=response.status_code if response.status_code in (400, 404, 409, 429) else 400,
            )

        if body and "total_amount" in body:
            new_amount = None
            try:
                new_amount = Decimal(str(body["total_amount"]))
            except (ValueError, TypeError):
                pass
            if new_amount is not None and new_amount != amount:
                amount = new_amount
                Transaction.objects.filter(transaction_id=tx_id).update(
                    amount=amount,
                    updated_at=timezone.now(),
                )

        transitioned = _set_status_with_event(
            tx_id,
            Transaction.Status.HOLDING,
            expected_status=Transaction.Status.STARTED,
            actor=SagaEvent.ACTOR_REQUEST,
            http_status_code=response.status_code,
            http_payload=body,
        )
        if not transitioned:
            return Response(
                {
                    "error": "Transaction was concurrently modified or resolved",
                    "transaction_id": str(tx_id),
                },
                status=409,
            )

        held_until = body.get("held_until") if body else None
        return Response(
            {
                "transaction_id": str(tx_id),
                "status": Transaction.Status.HOLDING,
                "held_until": held_until,
                "seat_ids": seat_ids,
                "seat_numbers": seat_numbers,
                "amount": str(amount),
                "quantity": quantity,
                "user_id": user_id,
                "user_email": user_email,
                "message": f"{quantity} seat(s) held. Complete payment before the hold expires.",
            },
            status=200,
        )

    # Phase 2: PAY
    if action == "pay":
        with transaction.atomic():
            txn = Transaction.objects.select_for_update().filter(transaction_id=pay_tid).first()
            if not txn:
                return Response(
                    {"error": "Transaction not found.", "code": "TRANSACTION_NOT_FOUND"},
                    status=404,
                )

            if not _can_access(txn, user_id, is_staff=is_staff):
                return Response(
                    {"error": "You do not have permission to pay for this transaction.", "code": "FORBIDDEN"},
                    status=403,
                )

            if txn.status == Transaction.Status.COMPLETED:
                return Response(_txn_json(txn), status=200)

            if txn.status != Transaction.Status.HOLDING:
                return Response(
                    {
                        "error": f"Transaction cannot be paid in status '{txn.status}'.",
                        "code": "INVALID_TRANSACTION_STATUS",
                        "status": txn.status,
                        "transaction_id": str(txn.transaction_id),
                    },
                    status=409,
                )

            holding_event = (
                SagaEvent.objects.filter(
                    transaction=txn,
                    to_status=Transaction.Status.HOLDING,
                )
                .order_by("-seq")
                .first()
            )
            payload = holding_event.http_payload if (holding_event and holding_event.http_payload) else {}
            raw_hold = payload.get("held_until")
            held_until_dt = parse_datetime(raw_hold) if isinstance(raw_hold, str) else raw_hold
            if hasattr(held_until_dt, "tzinfo") and timezone.is_naive(held_until_dt):
                held_until_dt = timezone.make_aware(held_until_dt)

            if hasattr(held_until_dt, "tzinfo") and held_until_dt < timezone.now():
                try:
                    _set_status_with_event(
                        txn.transaction_id,
                        Transaction.Status.TIMEOUT,
                        expected_status=Transaction.Status.HOLDING,
                        actor=SagaEvent.ACTOR_REQUEST,
                        http_status_code=409,
                        http_payload={"error": "Seat hold expired before payment attempt"},
                    )
                except Exception as exc:
                    logger.warning("Failed to record TIMEOUT for expired hold %s: %s", txn.transaction_id, exc)

                _release_inventory(txn.transaction_id, txn.seat_ids, correlation_id, "for expired hold")
                return Response(
                    {
                        "error": "Seat hold has expired. Please select seats and try again.",
                        "code": "HOLD_EXPIRED",
                        "status": Transaction.Status.TIMEOUT,
                        "transaction_id": str(txn.transaction_id),
                    },
                    status=409,
                )

            tx_id = txn.transaction_id
            seat_ids = txn.seat_ids
            amount = txn.amount
            pay_user_id = txn.user_id
            pay_user_email = txn.user_email

            if simulate_decline and enable_test_payment_failure:
                amount = Decimal("9999")

            transitioned = _set_status_with_event(
                tx_id,
                Transaction.Status.INVENTORY_RESERVED,
                expected_status=Transaction.Status.HOLDING,
                actor=SagaEvent.ACTOR_REQUEST,
                http_status_code=200,
                http_payload={
                    "message": f"{len(seat_ids) if seat_ids else 1} Seats reserved via coordinator hold",
                    "seat_ids": seat_ids,
                    "transaction_id": str(tx_id),
                },
            )
            if not transitioned:
                return Response(
                    {
                        "error": "Transaction is already being processed or is not in an eligible state for payment.",
                        "code": "CONCURRENT_PAYMENT_IN_PROGRESS",
                        "transaction_id": str(tx_id),
                    },
                    status=409,
                )

        if not is_sync:
            try:
                from .tasks import execute_saga_task
                execute_saga_task.delay(
                    str(tx_id),
                    seat_ids=seat_ids,
                    amount=str(amount),
                    fail_compensation=fail_compensation,
                    correlation_id=correlation_id,
                    user_id=pay_user_id,
                    user_email=pay_user_email,
                    skip_reserve=True,
                    simulate_decline=simulate_decline,
                )
                return Response(
                    {
                        "transaction_id": str(tx_id),
                        "status": "PENDING",
                        "poll_url": f"/transactions/{tx_id}/",
                        "stream_url": f"/transactions/{tx_id}/stream/",
                        "message": "Saga dispatched to Celery worker",
                    },
                    status=202,
                    headers={"Location": f"/transactions/{tx_id}/"},
                )
            except Exception:
                logger.exception("Failed to dispatch Saga task for pay action")
                _set_status_with_event(
                    tx_id,
                    to_status=Transaction.Status.HOLDING,
                    expected_status=Transaction.Status.INVENTORY_RESERVED,
                    actor=SagaEvent.ACTOR_REQUEST,
                    http_status_code=None,
                    http_payload={"error": "Celery dispatch failed; reverted to HOLDING"},
                )
                return Response(
                    {
                        "transaction_id": str(tx_id),
                        "status": Transaction.Status.HOLDING,
                        "error": "Saga could not be dispatched",
                    },
                    status=503,
                )

        return _run_saga(
            transaction_id=tx_id,
            seat_ids=seat_ids,
            amount=amount,
            fail_compensation=fail_compensation,
            correlation_id=correlation_id,
            user_id=pay_user_id,
            user_email=pay_user_email,
            skip_reserve=True,
            simulate_decline=simulate_decline,
        )

    # One-shot legacy full saga path
    tx_id = str(uuid.uuid4())
    amount, seat_ids, seat_numbers, price_error = _get_prices(
        seat_ids=seat_ids,
        correlation_id=correlation_id,
    )
    if price_error:
        return price_error

    quantity = len(seat_ids)
    if simulate_decline and enable_test_payment_failure:
        amount = Decimal("9999")

    created, dup_response = _create_transaction(
        transaction_id=tx_id,
        seat_ids=seat_ids,
        seat_numbers=seat_numbers,
        amount=amount,
        quantity=quantity,
        user_id=user_id,
        user_email=user_email,
    )
    if not created:
        return dup_response

    # Simulated coordinator crash test trigger
    if _faults_on() and request.data and (request.data.get("coordinator") or request.data.get("fail_at_step") == "coordinator"):
        return Response(
            {
                "transaction_id": str(tx_id),
                "user_id": user_id,
                "user_email": user_email,
                "status": "STARTED",
                "error": "Coordinator process crashed mid-flight (simulated)",
                "orphaned": True,
            },
            status=500,
        )

    if not is_sync:
        try:
            from .tasks import execute_saga_task
            execute_saga_task.delay(
                str(tx_id),
                seat_ids=seat_ids,
                amount=str(amount),
                fail_compensation=fail_compensation,
                correlation_id=correlation_id,
                user_id=user_id,
                user_email=user_email,
                simulate_decline=simulate_decline,
            )
            return Response(
                {
                    "transaction_id": str(tx_id),
                    "user_id": user_id,
                    "user_email": user_email,
                    "status": "PENDING",
                    "poll_url": f"/transactions/{tx_id}/",
                    "stream_url": f"/transactions/{tx_id}/stream/",
                    "message": "Saga dispatched to Celery worker",
                },
                status=202,
                headers={"Location": f"/transactions/{tx_id}/"},
            )
        except Exception:
            logger.exception("Failed to dispatch Saga task")
            _set_status_with_event(
                tx_id,
                to_status=Transaction.Status.TIMEOUT,
                expected_status=Transaction.Status.STARTED,
                actor=SagaEvent.ACTOR_REQUEST,
                http_status_code=None,
                http_payload={"error": "Celery dispatch failed; marked TIMEOUT"},
            )
            return Response(
                {
                    "transaction_id": str(tx_id),
                    "status": Transaction.Status.TIMEOUT,
                    "error": "Saga could not be dispatched",
                },
                status=503,
            )

    return _run_saga(
        transaction_id=tx_id,
        seat_ids=seat_ids,
        amount=amount,
        fail_compensation=fail_compensation,
        correlation_id=correlation_id,
        actor=SagaEvent.ACTOR_REQUEST,
        user_id=user_id,
        user_email=user_email,
        simulate_decline=simulate_decline,
    )


def _list_txns(request):
    """List transactions with pagination, filtering, and IDOR scoping."""
    is_authenticated, auth_user_id, is_staff = _get_caller(request)
    if not is_authenticated:
        return Response({"error": "Authentication required."}, status=401)

    limit_param = request.GET.get("limit", 10)
    offset_param = request.GET.get("offset", 0)
    page_param = request.GET.get("page")
    status_filter = request.GET.get("status")

    try:
        limit = max(1, min(int(limit_param), 100))
    except (ValueError, TypeError):
        limit = 10

    if page_param:
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

    if not is_staff:
        if auth_user_id:
            queryset = queryset.filter(user_id=auth_user_id)
        else:
            return Response({"count": 0, "limit": limit, "offset": offset, "results": []})

    if status_filter:
        queryset = queryset.filter(status=status_filter)

    total_count = queryset.count()
    txs = queryset[offset:offset + limit]
    data = [_txn_json(t) for t in txs]

    return Response({
        "count": total_count,
        "limit": limit,
        "offset": offset,
        "results": data,
    })


# =============================================================================
# SAGA ORCHESTRATION PIPELINE
# =============================================================================

def _run_saga(
    transaction_id,
    seat_ids=None,
    amount=None,
    fail_compensation=False,
    correlation_id=None,
    actor=SagaEvent.ACTOR_REQUEST,
    user_id=None,
    user_email=None,
    skip_reserve=False,
    simulate_decline=False,
    is_sync=False,
    **kwargs,
):
    """Execute the core distributed Saga pipeline."""
    if skip_reserve:
        current_status = (
            Transaction.objects.filter(transaction_id=transaction_id)
            .values_list("status", flat=True)
            .first()
        )
        if current_status is None:
            return Response(
                {
                    "error": "Transaction not found.",
                    "code": "TRANSACTION_NOT_FOUND",
                    "transaction_id": str(transaction_id),
                },
                status=404,
            )

        if current_status == Transaction.Status.HOLDING:
            transitioned = _set_status_with_event(
                transaction_id,
                Transaction.Status.INVENTORY_RESERVED,
                expected_status=Transaction.Status.HOLDING,
                actor=actor,
                http_status_code=200,
                http_payload={
                    "message": f"{len(seat_ids) if seat_ids else 1} Seats reserved via coordinator hold",
                    "seat_ids": seat_ids,
                    "transaction_id": str(transaction_id),
                },
            )
            if not transitioned:
                return Response(
                    {
                        "error": "Transaction is already being processed or is not in an eligible state for payment.",
                        "code": "CONCURRENT_PAYMENT_IN_PROGRESS",
                        "transaction_id": str(transaction_id),
                    },
                    status=409,
                )
        elif current_status != Transaction.Status.INVENTORY_RESERVED:
            return Response(
                {
                    "error": "Transaction is already being processed or is not in an eligible state for payment.",
                    "code": "CONCURRENT_PAYMENT_IN_PROGRESS",
                    "transaction_id": str(transaction_id),
                },
                status=409,
            )

        transitioned_pay = _set_status_with_event(
            transaction_id,
            Transaction.Status.PAYMENT_PROCESSING,
            expected_status=Transaction.Status.INVENTORY_RESERVED,
            actor=actor,
            http_status_code=None,
            http_payload=None,
        )
        if not transitioned_pay:
            return Response(
                {
                    "error": "Transaction was concurrently modified or resolved",
                    "code": "CONCURRENT_PAYMENT_IN_PROGRESS",
                    "transaction_id": str(transaction_id),
                },
                status=409,
            )
        return _charge(
            transaction_id,
            amount,
            fail_compensation,
            correlation_id=correlation_id,
            seat_ids=seat_ids,
            user_id=user_id,
            user_email=user_email,
        )

    response, reserve_error = _reserve(
        transaction_id,
        seat_ids,
        correlation_id=correlation_id,
        user_id=user_id,
        user_email=user_email,
    )
    if reserve_error:
        return reserve_error

    if response.status_code != 200:
        body = _parse_json_response(response)
        transitioned = _set_status_with_event(
            transaction_id,
            Transaction.Status.INVENTORY_RESERVE_FAILED,
            expected_status=Transaction.Status.STARTED,
            actor=actor,
            http_status_code=response.status_code,
            http_payload=body,
        )
        if not transitioned:
            return Response(
                {
                    "error": "Transaction was concurrently modified or resolved",
                    "transaction_id": str(transaction_id),
                },
                status=409,
            )
        return Response(
            {
                "transaction_id": str(transaction_id),
                "error": Transaction.Status.INVENTORY_RESERVE_FAILED,
                "inventory_response": body,
            },
            status=400,
        )

    body = _parse_json_response(response)
    if body and "total_amount" in body:
        actual_amount = None
        try:
            actual_amount = Decimal(str(body["total_amount"]))
        except (ValueError, TypeError):
            pass
        if actual_amount is not None and not simulate_decline:
            amount = actual_amount
            txn = Transaction.objects.filter(transaction_id=transaction_id).first()
            if txn:
                txn.amount = amount
                txn.save(update_fields=["amount"])

    transitioned_res = _set_status_with_event(
        transaction_id,
        Transaction.Status.INVENTORY_RESERVED,
        expected_status=Transaction.Status.STARTED,
        actor=actor,
        http_status_code=response.status_code,
        http_payload=body,
    )
    if not transitioned_res:
        return Response(
            {
                "error": "Transaction was concurrently modified or resolved",
                "transaction_id": str(transaction_id),
            },
            status=409,
        )

    transitioned_pay = _set_status_with_event(
        transaction_id,
        Transaction.Status.PAYMENT_PROCESSING,
        expected_status=Transaction.Status.INVENTORY_RESERVED,
        actor=actor,
        http_status_code=None,
        http_payload=None,
    )
    if not transitioned_pay:
        return Response(
            {
                "error": "Transaction was concurrently modified or resolved",
                "transaction_id": str(transaction_id),
            },
            status=409,
        )

    return _charge(
        transaction_id,
        amount,
        fail_compensation,
        correlation_id=correlation_id,
        seat_ids=seat_ids,
        user_id=user_id,
        user_email=user_email,
    )


# =============================================================================
# MICROSERVICE REMOTE CALLS
# =============================================================================

def _reserve(transaction_id, seat_ids, correlation_id=None, user_id=None, user_email=None):
    """Call downstream Inventory service to reserve seats."""
    headers = {
        "X-Correlation-ID": _corr_id(correlation_id),
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
        "X-Idempotency-Key": str(transaction_id),
    }

    try:
        response = _http_post(
            INVENTORY_RESERVE_URL,
            json={
                "seat_ids": seat_ids,
                "transaction_id": str(transaction_id),
                "user_id": user_id,
                "user_email": user_email,
            },
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        return response, None
    except RequestException as exc:
        status = _err_status(
            exc, Transaction.Status.INVENTORY_TIMEOUT, Transaction.Status.INVENTORY_CONNECTION_ERROR
        )
        transitioned = _set_status_with_event(
            transaction_id,
            status,
            expected_status=Transaction.Status.STARTED,
            actor=SagaEvent.ACTOR_REQUEST,
            http_status_code=None,
            http_payload=None,
        )
        if not transitioned:
            return None, Response(
                {
                    "error": "Transaction was concurrently modified or resolved",
                    "transaction_id": str(transaction_id),
                },
                status=409,
            )
        return None, Response({"transaction_id": str(transaction_id), "status": status}, status=504)


def _get_coordinator_redis_client():
    """Return a Redis client instance from Celery task configuration."""
    try:
        from coordinator.tasks import get_redis_client
        return get_redis_client()
    except Exception:
        return None


def _set_checkout_presence(transaction_id):
    """Set Redis checkout presence key with TTL to protect seat holds during payment."""
    try:
        client = _get_coordinator_redis_client()
        if client:
            ttl = int(REQUEST_READ_TIMEOUT + 60)
            client.set(f"dtc:checkout:{transaction_id}", "1", ex=ttl)
    except Exception as exc:
        logger.warning("Failed to set checkout presence key for tx %s: %s", transaction_id, exc)


def _clear_checkout_presence(transaction_id):
    """Clear Redis checkout presence key once transaction reaches terminal state."""
    try:
        client = _get_coordinator_redis_client()
        if client:
            client.delete(f"dtc:checkout:{transaction_id}")
    except Exception as exc:
        logger.warning("Failed to clear checkout presence key for tx %s: %s", transaction_id, exc)


def _charge(
    transaction_id,
    amount,
    fail_compensation,
    correlation_id=None,
    seat_ids=None,
    user_id=None,
    user_email=None,
):
    """Charge payment with presence lock protection."""
    _set_checkout_presence(transaction_id)
    try:
        return _do_charge(
            transaction_id,
            amount,
            fail_compensation,
            correlation_id=correlation_id,
            seat_ids=seat_ids,
            user_id=user_id,
            user_email=user_email,
        )
    except Exception:
        _clear_checkout_presence(transaction_id)
        raise


def _do_charge(
    transaction_id,
    amount,
    fail_compensation,
    correlation_id=None,
    seat_ids=None,
    user_id=None,
    user_email=None,
):
    """Execute payment charge HTTP call and handle transport or 5xx verification."""
    headers = {
        "X-Correlation-ID": _corr_id(correlation_id),
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
        "X-Idempotency-Key": str(transaction_id),
    }

    try:
        payment_response = _http_post(
            PAYMENT_CHARGE_URL,
            json={
                "transaction_id": str(transaction_id),
                "amount": str(amount),
                "user_id": user_id,
                "user_email": user_email,
            },
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except RequestException as exc:
        uncertain_status = _err_status(
            exc, Transaction.Status.PAYMENT_TIMEOUT, Transaction.Status.PAYMENT_CONNECTION_ERROR
        )
        transitioned = _set_status_with_event(
            transaction_id,
            uncertain_status,
            expected_status=Transaction.Status.PAYMENT_PROCESSING,
            actor=SagaEvent.ACTOR_REQUEST,
            http_status_code=None,
            http_payload=None,
        )
        if not transitioned:
            return Response(
                {
                    "error": "Transaction was concurrently modified or resolved",
                    "transaction_id": str(transaction_id),
                },
                status=409,
            )

        try:
            payment_check = _http_get(
                PAYMENT_CHECK_URL,
                params={"transaction_id": str(transaction_id)},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except RequestException:
            return Response(
                {
                    "transaction_id": str(transaction_id),
                    "status": uncertain_status,
                    "error": "Payment outcome could not be determined",
                },
                status=503,
            )

        check_data = _parse_json_response(payment_check)

        return _finish_payment(
            transaction_id,
            fail_compensation,
            check_data or {},
            payment_status_code=payment_check.status_code,
            correlation_id=correlation_id,
            seat_ids=seat_ids,
        )

    if payment_response.status_code >= 500:
        uncertain_status = Transaction.Status.PAYMENT_TIMEOUT
        transitioned = _set_status_with_event(
            transaction_id,
            uncertain_status,
            expected_status=Transaction.Status.PAYMENT_PROCESSING,
            actor=SagaEvent.ACTOR_REQUEST,
            http_status_code=payment_response.status_code,
            http_payload=_parse_json_response(payment_response),
        )
        if not transitioned:
            return Response(
                {
                    "error": "Transaction was concurrently modified or resolved",
                    "transaction_id": str(transaction_id),
                },
                status=409,
            )

        try:
            payment_check = _http_get(
                PAYMENT_CHECK_URL,
                params={"transaction_id": str(transaction_id)},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except RequestException:
            return Response(
                {
                    "transaction_id": str(transaction_id),
                    "status": uncertain_status,
                    "error": "Payment outcome could not be determined",
                },
                status=503,
            )

        check_data = _parse_json_response(payment_check) if payment_check.status_code == 200 else None
        if payment_check.status_code == 200:
            return _finish_payment(
                transaction_id,
                fail_compensation,
                check_data or {},
                payment_status_code=payment_check.status_code,
                correlation_id=correlation_id,
                seat_ids=seat_ids,
            )
        return Response(
            {
                "transaction_id": str(transaction_id),
                "status": uncertain_status,
                "error": "Payment outcome could not be determined",
            },
            status=503,
        )

    resp_data = _parse_json_response(payment_response)
    return _finish_payment(
        transaction_id,
        fail_compensation,
        resp_data or {},
        payment_status_code=payment_response.status_code,
        correlation_id=correlation_id,
        seat_ids=seat_ids,
    )


def _confirm(transaction_id, seat_ids=None, correlation_id=None):
    """Call downstream Inventory service to confirm booked seats."""
    if not seat_ids:
        txn = Transaction.objects.filter(transaction_id=transaction_id).first()
        if txn and txn.seat_ids:
            seat_ids = txn.seat_ids
    headers = {
        "X-Correlation-ID": _corr_id(correlation_id),
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
        "X-Idempotency-Key": str(transaction_id),
    }
    try:
        response = _http_post(
            INVENTORY_CONFIRM_URL,
            json={"seat_ids": seat_ids, "transaction_id": str(transaction_id)},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        return response, None
    except RequestException as exc:
        return None, exc


def _finish_payment(transaction_id, fail_compensation, payment_data, payment_status_code=200, correlation_id=None, seat_ids=None):
    """Finalize transaction following payment response (confirm inventory or rollback)."""
    if payment_status_code == 409:
        code = payment_data.get("code") if isinstance(payment_data, dict) else None
        if code == "IDEMPOTENCY_PAYLOAD_MISMATCH":
            logger.critical(
                "Idempotency conflict detected for transaction %s: %s",
                transaction_id,
                payment_data.get("error"),
            )
        elif code == "TRANSACTION_VOIDED":
            logger.warning(
                "Payment rejected because transaction %s was previously voided.",
                transaction_id,
            )

    if payment_data.get("status") == "CHARGED":
        confirm_resp, confirm_err = _confirm(transaction_id, seat_ids=seat_ids, correlation_id=correlation_id)

        # Retry confirm once on transport failure or 5xx before giving up
        if (confirm_resp is None and confirm_err) or (confirm_resp is not None and confirm_resp.status_code in (502, 503, 504)):
            logger.warning(
                "Inventory confirm timed out or returned gateway error for tx %s (%s); retrying confirm...",
                transaction_id,
                confirm_err or getattr(confirm_resp, "status_code", None),
            )
            confirm_resp, confirm_err = _confirm(transaction_id, seat_ids=seat_ids, correlation_id=correlation_id)

        if confirm_resp and confirm_resp.status_code == 200:
            confirm_json = _parse_json_response(confirm_resp)

            transitioned = _set_status_with_event(
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
            _clear_checkout_presence(transaction_id)
            if not transitioned:
                current = Transaction.objects.filter(transaction_id=transaction_id).first()
                curr_status = current.status if current else "UNKNOWN"
                return Response(
                    {
                        "error": "Transaction was concurrently modified or resolved",
                        "status": curr_status,
                        "transaction_id": str(transaction_id),
                    },
                    status=409,
                )
            txn = Transaction.objects.filter(transaction_id=transaction_id).first()
            data = _txn_json(txn) if txn else {
                "transaction_id": str(transaction_id),
                "status": Transaction.Status.COMPLETED,
            }
            data.update({
                "payment_response": payment_data,
                "inventory_response": confirm_json,
            })
            return Response(data)

        # Transport failure after retries: leave in PAYMENT_PROCESSING for recovery sweep
        if confirm_resp is None:
            logger.warning(
                "Inventory confirm timed out twice for tx %s; leaving in PAYMENT_PROCESSING for sweep retry.",
                transaction_id,
            )
            return Response(
                {
                    "transaction_id": str(transaction_id),
                    "status": Transaction.Status.PAYMENT_PROCESSING,
                    "error": "Inventory confirm timed out; will be retried by recovery sweep.",
                    "needs_recovery": True,
                },
                status=503,
            )

        confirm_json = _parse_json_response(confirm_resp)

        decline_payload = {
            "error": "Seat hold expired or confirmation failed",
            "payment": payment_data,
            "inventory_confirm": confirm_json if confirm_json is not None else {"error": "Inventory confirmation failed"},
        }
        return _compensate(
            transaction_id,
            fail_compensation,
            decline_payload,
            payment_status_code=confirm_resp.status_code,
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


def _compensate(transaction_id, fail_compensation, payment_data, payment_status_code=None, correlation_id=None, seat_ids=None):
    """Compensate failed transaction (void/refund payment and release reserved seats)."""
    tx_id_release = "fail-release" if fail_compensation else str(transaction_id)
    headers = {
        "X-Correlation-ID": _corr_id(correlation_id),
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
        "X-Idempotency-Key": str(transaction_id),
    }

    claimed = _set_status_with_event(
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
    if not claimed:
        current = Transaction.objects.filter(transaction_id=transaction_id).first()
        curr_status = current.status if current else "UNKNOWN"
        return Response(
            {
                "error": "Transaction was concurrently modified or resolved",
                "status": curr_status,
                "transaction_id": str(transaction_id),
            },
            status=409,
        )

    was_charged = isinstance(payment_data, dict) and (
        payment_data.get("status") == "CHARGED"
        or (isinstance(payment_data.get("payment"), dict) and payment_data["payment"].get("status") == "CHARGED")
    )

    # Fence payment by voiding in-flight charge before releasing inventory
    if not was_charged:
        void_ok = False
        try:
            void_resp = _http_post(
                PAYMENT_VOID_URL,
                json={"transaction_id": str(transaction_id)},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if void_resp.status_code == 200:
                v_data = _parse_json_response(void_resp)
                if v_data and v_data.get("status") == "CHARGED":
                    was_charged = True
                void_ok = True
        except RequestException:
            void_ok = False

        if not void_ok and not was_charged:
            logger.warning(
                "Payment void failed for tx %s during compensation; aborting seat release to prevent unfenced release.",
                transaction_id,
            )
            return Response(
                {
                    "transaction_id": str(transaction_id),
                    "status": Transaction.Status.COMPENSATING,
                    "error": "Payment void failed; compensation will be retried by recovery sweep.",
                    "needs_recovery": True,
                },
                status=503,
            )

    # 1. Release inventory
    release_ok, release_data, release_status_code, release_transport_error = False, None, None, None
    try:
        release_response = _http_post(
            INVENTORY_RELEASE_URL,
            json={"transaction_id": tx_id_release},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        release_status_code = release_response.status_code
        release_data = _parse_json_response(release_response)
        release_ok = (release_status_code == 200)
        if not release_ok and isinstance(release_data, dict):
            msg = str(release_data.get("message", "")).lower() + " " + str(release_data.get("error", "")).lower()
            code = str(release_data.get("code", "")).upper()
            if (
                "already released" in msg
                or "nothing to release" in msg
                or "holds may have already been cancelled or expired" in msg
                or code in ("ALREADY_RELEASED", "NOTHING_TO_RELEASE", "NOT_FOUND")
            ):
                release_ok = True
    except RequestException as exc:
        release_transport_error = exc

    # 2. Refund payment if charged
    refund_ok, refund_data, refund_status_code, refund_transport_error = not was_charged, None, None, None
    if was_charged:
        try:
            refund_response = _http_post(
                PAYMENT_REFUND_URL,
                json={"transaction_id": str(transaction_id)},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            refund_status_code = refund_response.status_code
            refund_ok = (refund_status_code == 200)
            refund_data = _parse_json_response(refund_response)
        except RequestException as exc:
            refund_transport_error = exc

    # 3. Finalize outcome
    if release_ok and refund_ok:
        transitioned = _set_status_with_event(
            transaction_id,
            Transaction.Status.COMPENSATED,
            expected_status=Transaction.Status.COMPENSATING,
            actor=SagaEvent.ACTOR_REQUEST,
            http_status_code=release_status_code or 200,
            http_payload=release_data,
        )
        _clear_checkout_presence(transaction_id)
        if not transitioned:
            current = Transaction.objects.filter(transaction_id=transaction_id).first()
            curr_status = current.status if current else "UNKNOWN"
            return Response(
                {
                    "error": "Transaction was concurrently modified or resolved",
                    "status": curr_status,
                    "transaction_id": str(transaction_id),
                },
                status=409,
            )
        txn = Transaction.objects.filter(transaction_id=transaction_id).first()
        data = _txn_json(txn) if txn else {
            "transaction_id": str(transaction_id),
            "status": Transaction.Status.COMPENSATED,
        }
        data.update({
            "payment_response": payment_data,
            "compensation_response": release_data,
        })
        if refund_data:
            data["refund_response"] = refund_data
        return Response(data, status=400)

    # Partial or complete failure during compensation
    transitioned = _set_status_with_event(
        transaction_id,
        Transaction.Status.COMPENSATION_FAILED,
        expected_status=Transaction.Status.COMPENSATING,
        actor=SagaEvent.ACTOR_REQUEST,
        http_status_code=refund_status_code if not refund_ok else release_status_code,
        http_payload=refund_data if not refund_ok else release_data,
    )
    _clear_checkout_presence(transaction_id)
    if not transitioned:
        current = Transaction.objects.filter(transaction_id=transaction_id).first()
        curr_status = current.status if current else "UNKNOWN"
        return Response(
            {
                "error": "Transaction was concurrently modified or resolved",
                "status": curr_status,
                "transaction_id": str(transaction_id),
            },
            status=409,
        )
    txn = Transaction.objects.filter(transaction_id=transaction_id).first()
    data = _txn_json(txn) if txn else {
        "transaction_id": str(transaction_id),
        "status": Transaction.Status.COMPENSATION_FAILED,
    }
    data.update({
        "payment_response": payment_data,
        "compensation_response": release_data,
    })
    if refund_data:
        data["refund_response"] = refund_data

    if release_transport_error or refund_transport_error:
        active_err = release_transport_error or refund_transport_error
        cause = _err_status(active_err, Transaction.Status.TIMEOUT, Transaction.Status.CONNECTION_FAILED)
        data["cause"] = cause
        data["error"] = "Network error during compensation"
        return Response(data, status=503)

    data["error"] = "Compensation failed"
    return Response(data, status=500)


# =============================================================================
# SERIALIZATION, IDENTITY & INFRASTRUCTURE HELPERS
# =============================================================================

def _is_admin_email(email):
    """Check if email belongs to the configured ADMIN_DOMAIN."""
    if not email or not isinstance(email, str):
        return False
    admin_domain = os.getenv("ADMIN_DOMAIN", "").strip().lower()
    if not admin_domain:
        return False
    admin_domain = admin_domain.removeprefix("@")
    if not admin_domain:
        return False

    parts = email.strip().lower().split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    return parts[1] == admin_domain


class RequesterIdentity(tuple):
    """Caller identity 3-tuple (is_authenticated, user_id, is_staff) with .user_email."""

    def __new__(cls, is_authenticated, user_id, is_staff, user_email=None):
        instance = super().__new__(cls, (is_authenticated, user_id, is_staff))
        instance.is_authenticated = is_authenticated
        instance.user_id = user_id
        instance.is_staff = is_staff
        instance.user_email = user_email
        return instance


def _get_caller(request):
    """Resolve caller authentication and staff privileges."""
    if _verify_staff_key(request.headers.get("X-Staff-Key")):
        user_id = request.headers.get("X-User-ID") or os.getenv("STAFF_USER_ID") or "system_staff"
        user_email = request.headers.get("X-User-Email") or os.getenv("STAFF_EMAIL_ID") or "admin@dtc.internal"
        return RequesterIdentity(True, user_id, True, user_email=user_email)

    user_info = get_user_from_request(request)
    if user_info and user_info.get("is_authenticated"):
        user_email = user_info.get("user_email")
        is_staff = _is_admin_email(user_email)
        return RequesterIdentity(True, user_info.get("user_id"), is_staff, user_email=user_email)

    if getattr(settings, "IS_TESTING", False):
        test_uid = request.headers.get("X-User-ID") or "system_test"
        test_email = request.headers.get("X-User-Email") or None
        is_staff = _is_admin_email(test_email) if test_email else True
        return RequesterIdentity(True, test_uid, is_staff, user_email=test_email)

    is_dev = getattr(settings, "DEBUG", False)
    allow_dev_staff_bypass = getattr(settings, "ALLOW_DEV_STAFF_BYPASS", False) or (
        os.getenv("ALLOW_DEV_STAFF_BYPASS", "").lower() in ("true", "1", "yes", "t")
    )
    is_dev_staff = bool(is_dev and allow_dev_staff_bypass)

    if is_dev:
        dev_uid = request.headers.get("X-User-ID") or (user_info.get("user_id") if user_info else "system_dev")
        dev_email = request.headers.get("X-User-Email") or (user_info.get("user_email") if user_info else None)
        return RequesterIdentity(True, dev_uid, is_dev_staff, user_email=dev_email)

    return RequesterIdentity(False, None, False, user_email=None)


def _create_transaction(transaction_id, seat_ids=None, seat_numbers=None, amount=500, quantity=1, user_id=None, user_email=None):
    """Atomically create a new Transaction row with status=STARTED."""
    try:
        with transaction.atomic():
            Transaction.objects.create(
                transaction_id=transaction_id,
                user_id=user_id,
                user_email=user_email,
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
                "status": existing.status,
            },
            status=200,
        )
    except Exception as exc:
        logger.error(f"Failed to create transaction record: {exc}", exc_info=True)
        return False, Response(
            {
                "error": "Database error creating transaction record",
                "code": "DB_ERROR",
            },
            status=500,
        )


def _txn_json(txn):
    """Serialize Transaction model instance to JSON-serializable dict."""
    return {
        "transaction_id": str(txn.transaction_id),
        "user_id": txn.user_id,
        "user_email": txn.user_email,
        "status": txn.status,
        "is_terminal": txn.status in Transaction.TERMINAL_STATUSES,
        "needs_recovery": txn.status in Transaction.RECOVERABLE_STATUSES,
        "seat_ids": txn.seat_ids,
        "seat_numbers": txn.seat_numbers,
        "quantity": txn.quantity,
        "amount": str(txn.amount),
        "created_at": txn.created_at,
        "updated_at": txn.updated_at,
    }


def _event_json(event):
    """Serialize SagaEvent model instance to JSON-serializable dict."""
    return {
        "seq": event.seq,
        "from_status": event.from_status,
        "to_status": event.to_status,
        "actor": event.actor,
        "http_status_code": event.http_status_code,
        "http_payload": event.http_payload,
        "created_at": event.created_at,
    }


def _err_status(exc, timeout_status, conn_status):
    """Map request exception type to corresponding transaction status."""
    return timeout_status if isinstance(exc, Timeout) else conn_status


def _parse_json_response(response):
    """Safely decode JSON payload from HTTP response."""
    try:
        return response.json()
    except (ValueError, AttributeError):
        return None
