import json
import logging
import os
import secrets
import time
import uuid
import redis
import requests
from decimal import Decimal

from rest_framework.decorators import api_view
from rest_framework.response import Response
from requests.exceptions import Timeout, ConnectionError
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import StreamingHttpResponse, JsonResponse, HttpResponse

from .models import Transaction, SagaEvent
from .saga_log import _set_status_with_event
from .middleware import get_correlation_id
from coordinator.auth_views import get_user_from_request

logger = logging.getLogger(__name__)


def _get_active_correlation_id(correlation_id=None):
    """
    Returns an active correlation ID, prioritizing:
      1. Explicit correlation_id argument passed to helper
      2. ContextVar correlation_id stored in current thread by CorrelationIdMiddleware
      3. Newly generated UUIDv4 string
    Observability tracing is strictly decoupled from domain entity transaction IDs.
    """
    return correlation_id or get_correlation_id() or str(uuid.uuid4())



# ==============================================================================
# SECTION 1: CONFIGURATION & SERVICE ENDPOINTS
# URLs, timeouts, and authentication keys used to communicate with peer services.
# ==============================================================================>

INVENTORY_BASE_URL = os.getenv("INVENTORY_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
PAYMENT_BASE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")

INVENTORY_RESERVE_URL = f"{INVENTORY_BASE_URL}/seats/reserve/"
INVENTORY_CONFIRM_URL = f"{INVENTORY_BASE_URL}/seats/confirm/"
INVENTORY_RELEASE_URL = f"{INVENTORY_BASE_URL}/seats/release/"
INVENTORY_PRICING_URL = f"{INVENTORY_BASE_URL}/seats/pricing/"
PAYMENT_CHARGE_URL = f"{PAYMENT_BASE_URL}/payments/charge/"
PAYMENT_CHECK_URL = f"{PAYMENT_BASE_URL}/payments/check/"
PAYMENT_REFUND_URL = f"{PAYMENT_BASE_URL}/payments/refund/"

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "5"))
INTERNAL_SERVICE_KEY = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY")

from requests.adapters import HTTPAdapter

_HTTP_SESSION = None

def _get_http_session():
    global _HTTP_SESSION
    if _HTTP_SESSION is None:
        _HTTP_SESSION = requests.Session()
        adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25)
        _HTTP_SESSION.mount("http://", adapter)
        _HTTP_SESSION.mount("https://", adapter)
    return _HTTP_SESSION

def _http_post(url, **kwargs):
    # Support unittest.mock patches in test suite
    if hasattr(requests.post, "mock_calls") or hasattr(requests.post, "assert_called"):
        return requests.post(url, **kwargs)
    return _get_http_session().post(url, **kwargs)

def _http_get(url, **kwargs):
    # Support unittest.mock patches in test suite
    if hasattr(requests.get, "mock_calls") or hasattr(requests.get, "assert_called"):
        return requests.get(url, **kwargs)
    return _get_http_session().get(url, **kwargs)


# ==============================================================================
# SECTION 2: PUBLIC API ENDPOINTS (URL VIEW TARGETS)
# These endpoints are mapped directly in urls.py for incoming HTTP requests.
# ==============================================================================

@api_view(["GET"])
def health_check(request):
    # Runs every 15 seconds
    return Response({"status": "healthy", "service": "coordinator"})


@api_view(["GET", "POST"])
def transactions_endpoint(request):
    if request.method == "GET":
        return _handle_list_transactions(request)
    # POST
    return _handle_create_transaction(request)


@api_view(["GET"])
def list_transactions(request):
    return _handle_list_transactions(request)


@api_view(["GET"])
def get_transaction(request, transaction_id):
    is_authenticated, auth_user_id, is_staff = _get_requester_identity(request)
    if not is_authenticated:
        return Response({"error": "Authentication required."}, status=401)

    try:
        txn = Transaction.objects.get(transaction_id=transaction_id)
    except Transaction.DoesNotExist:
        return Response({"detail": "Transaction not found"}, status=404)

    # IDOR protection: Non-staff users may only view their own transactions
    if not is_staff and (not auth_user_id or (txn.user_id and txn.user_id != auth_user_id)):
        return Response({"error": "You do not have permission to view this transaction."}, status=403)

    return Response(_serialize_transaction(txn))


@api_view(["GET"])
def list_transaction_events(request, transaction_id):
    is_authenticated, auth_user_id, is_staff = _get_requester_identity(request)
    if not is_authenticated:
        return Response({"error": "Authentication required."}, status=401)

    try:
        txn = Transaction.objects.get(transaction_id=transaction_id)
    except Transaction.DoesNotExist:
        return Response({"detail": "Transaction not found", "events": []}, status=404)

    # IDOR protection: Non-staff users may only view their own transaction events
    if not is_staff and (not auth_user_id or (txn.user_id and txn.user_id != auth_user_id)):
        return Response({"error": "You do not have permission to view this transaction's events."}, status=403)

    events = txn.events.order_by("seq")
    return Response({
        "transaction_id": str(txn.transaction_id),
        "status": txn.status,
        "events": [_serialize_event(e) for e in events],
    })


def transaction_stream(request, transaction_id):
    """
    Server-Sent Events (SSE) streaming endpoint for real-time transaction state updates.
    Eliminates client-side polling by streaming state transitions directly via Redis Pub/Sub.
    """
    if request.method == "OPTIONS":
        return HttpResponse(status=204)

    is_authenticated, auth_user_id, is_staff = _get_requester_identity(request)
    if not is_authenticated:
        return JsonResponse({"error": "Authentication required."}, status=401)

    try:
        txn = Transaction.objects.get(transaction_id=transaction_id)
    except Transaction.DoesNotExist:
        return JsonResponse({"detail": "Transaction not found"}, status=404)

    # IDOR protection: Non-staff users may only stream their own transactions
    if not is_staff and (not auth_user_id or (txn.user_id and txn.user_id != auth_user_id)):
        return JsonResponse({"error": "You do not have permission to view this transaction."}, status=403)

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
    }

    def event_generator():
        channel_name = f"dtc:tx_stream:{transaction_id}"
        broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")

        redis_client = None
        pubsub = None
        try:
            # Step 1: Subscribe to Redis channel FIRST to prevent any race conditions
            redis_client = redis.Redis.from_url(
                broker_url,
                decode_responses=True,
                socket_connect_timeout=2.0,
            )

            # Normaly redis is a storage where we either set or get key
            # but we insist redis to not treat this like a storage right now,
            # and create a walkie-talkie (Pub/Sub)for us where i can listen for announcements.
            # pubsub is simply the variable holding that open walkie-talkie
            pubsub = redis_client.pubsub()

            # Now that we have the walkie-talkie what channel should we listen to?
            # below line means, tune my walkie-talkie to channel # channel_name

            """
            Why we need this?

            Imagine 500 different people are buying tickets on my website at the same time:

            User A is buying Seat A1.
            User B is buying Seat B5.
            You are buying Seat C10.
            If your walkie-talkie listened to everything, you would hear updates about User A's credit card and User B's credit card!

            So the computer gives your transaction a unique channel name (like dtc:tx_stream:order-999).

            pubsub.subscribe(channel_name) makes sure your walkie-talkie only listens to updates about YOUR order, completely ignoring everyone else.
            """
            pubsub.subscribe(channel_name)

            # Step 2: Query PostgreSQL for current state
            current_txn = Transaction.objects.filter(transaction_id=transaction_id).first()
            if current_txn:
                is_term = current_txn.status in terminal_statuses
                initial_msg = json.dumps({
                    "transaction_id": str(current_txn.transaction_id),
                    "status": current_txn.status,
                    "is_terminal": is_term,
                })
                yield f"data: {initial_msg}\n\n"
                if is_term:
                    return

            # Step 3: Stream updates as Celery publishes them
            start_time = time.monotonic()
            max_duration = 30.0  # 30-second safety timeout
            last_db_check = start_time

            while True:
                now = time.monotonic()
                if now - start_time > max_duration:
                    timeout_msg = json.dumps({
                        "transaction_id": str(transaction_id),
                        "status": "TIMEOUT",
                        "is_terminal": True,
                    })
                    yield f"data: {timeout_msg}\n\n"
                    break

                # Non-blocking check with 1-second timeout
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
                    # Reverse proxy keep-alive heartbeat comment
                    yield ": ping\n\n"

                    # Safeguard: periodic DB check every 3 seconds in case Redis message was missed
                    if now - last_db_check >= 3.0:
                        last_db_check = now
                        db_txn = Transaction.objects.filter(transaction_id=transaction_id).first()

                        # If redis had a network hiccup or restarted right as celery finished
                        # then for every three seconds of silence, this code directly checks PostgresSQL
                        # If the txn status is in terminal_statuses then i send the finish message to browser
                        # and closes the stream(break).
                        if db_txn and db_txn.status in terminal_statuses:
                            term_msg = json.dumps({
                                "transaction_id": str(db_txn.transaction_id),
                                "status": db_txn.status,
                                "is_terminal": True,
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

    response = StreamingHttpResponse(event_generator(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    response["Connection"] = "keep-alive"
    return response


@api_view(["POST"])
def retry_compensation(request, transaction_id):
    # Public reach blocked
    is_authenticated, user_id, is_staff = _get_requester_identity(request)

    if not is_authenticated:
        return Response({"Status": "Not Authenticated"}, status=401)
    
    txn = Transaction.objects.exclude(
        status=Transaction.Status.COMPLETED
    ).filter(
        transaction_id=transaction_id
    ).first()

    if not txn:
        return Response({
            "detail": "Transaction is already charged and marked as completed"
        })

    # IDOR protection: Non-staff callers can only retry compensation on their own transactions
    if not is_staff and (not user_id or (txn.user_id and txn.user_id != user_id)):
        return Response(
            {"error": "You do not have permission to retry compensation for this transaction."},
            status=403,
        )
    
    txn_status_at_entry = txn.status
    
    if txn_status_at_entry == Transaction.Status.COMPENSATED:
        return Response({
            "transaction_id": str(transaction_id),
            "status": Transaction.Status.COMPENSATED,
            "message": "Transaction is already compensated",
        })

    headers = {
        "X-Correlation-ID": _get_active_correlation_id(getattr(request, "correlation_id", None)),
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
    }
    try:
        release_response = _http_post(
            INVENTORY_RELEASE_URL,
            json={"transaction_id": str(transaction_id), "seat_ids": txn.seat_ids},
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

    release_data = _parse_json_response(release_response)

    if release_response.status_code == 200:
        transitioned = _set_status_with_event(
            transaction_id,
            to_status=Transaction.Status.COMPENSATED_ON_RETRY,
            expected_status=txn_status_at_entry,
            actor=SagaEvent.ACTOR_RETRY,
            http_status_code=release_response.status_code,
            http_payload=release_data,
        )

        if not transitioned:
            current = Transaction.objects.get(transaction_id=transaction_id)
            return Response({
                "transaction_id": str(transaction_id),
                "status": current.status,
                "message": "Transaction was concurrently resolved by another process",
                "compensation_response": release_data,
            })

        return Response({
            "transaction_id": str(transaction_id),
            "status": Transaction.Status.COMPENSATED_ON_RETRY,
            "compensation_response": release_data,
        })

    _set_status_with_event(
        transaction_id,
        to_status=Transaction.Status.RETRY_COMPENSATION_FAILED,
        expected_status=txn_status_at_entry,
        actor=SagaEvent.ACTOR_RETRY,
        http_status_code=release_response.status_code,
        http_payload=release_data,
    )
    return Response(
        {
            "transaction_id": str(transaction_id),
            "status": Transaction.Status.RETRY_COMPENSATION_FAILED,
            "compensation_response": release_data,
        },
        status=500,
    )


# this endpoint allows an authenticated admin or the frontend visualizer to manually trigger an immediate crash-recovery sweep on demand.
@api_view(["POST"])
def trigger_recovery_sweep(request):
    import io
    from django.core.management import call_command
    from django.conf import settings

    is_dev = getattr(settings, "DEBUG", False)
    is_testing = bool(getattr(settings, "IS_TESTING", False))

    if not (is_dev or is_testing):
        is_authenticated, auth_user_id, is_staff = _get_requester_identity(request)
        if not is_authenticated:
            return Response({"error": "Authentication required to trigger recovery sweep."}, status=401)
        if not is_staff:
            return Response({"error": "Staff privileges required to trigger recovery sweep."}, status=403)

    """
    1. Management commands normally print their logs directly to the command line terminal (stdout).

    2. io.StringIO() creates an in-memory text buffer.

    3. When passed to stdout=out, all the text that would normally be printed in the terminal is redirected into 
    this buffer so it can be sent back in the HTTP JSON response!
    """
    out = io.StringIO()

    # threshold_seconds => How long a transaction must be sitting in an unfinished state before the sweeper considers it "abandoned/crashed" (default: 60 seconds).
    raw_threshold = int(request.data.get("threshold_seconds", 60) if request.data else 60)


    # What if a caller sends threshold_seconds = 0 or 1
    # The sweeper would mistakenly abort and kill legitimate transactions that are actively processing payments right now.
    # This enforces a minimum floor of atleast 30 seconds.    
    min_floor = 5 if getattr(settings, "DEBUG", False) else int(os.getenv("RECOVERY_MIN_THRESHOLD_SECONDS", "30"))


    threshold_seconds = max(raw_threshold, min_floor)

    # This is Django’s programmatic way of running:
    #   python manage.py recovery_stuck_transactions --threshold_seconds=30
    call_command("recovery_stuck_transactions", threshold_seconds=threshold_seconds, stdout=out)

    try:
        import time
        from coordinator.tasks import get_redis_client
        client = get_redis_client()

        # Update timestamp because a sweep just finished.
        client.set("dtc:recovery:last_run_timestamp", str(time.time()))
    except Exception:
        pass

    return Response({
        "message": "Recovery sweep complete",
        "threshold_seconds_applied": threshold_seconds,
        "output": out.getvalue().strip(),
    })


@api_view(["GET"])
def recovery_status(request):
    import time
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
        # time since last run
        # If elapsed time is 30 seconds, then (elapsed % cycle) = (30 % 90) = 30, 90 - 30 = 60 seconds remaing
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


# ==============================================================================
# SECTION 3: CONTROLLER REQUEST HANDLERS
# Internal handlers that authenticate, parse, and validate incoming requests.
# ==============================================================================

def _resolve_seat_pricing(seat_ids=None, correlation_id=None):
    """
    Resolves official seat pricing and details from the Inventory Service DB.
    Matches seats strictly by seat_ids and constructs human-readable seat numbers
    from the retrieved inventory seat records.
    Returns: (total_amount, resolved_seat_ids, resolved_seat_numbers, error_response)
    """

    # 2. Query Inventory Service for seat pricing — POST with JSON body {"seat_ids": [1, 2]}
    headers = {
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
        "X-Correlation-ID": _get_active_correlation_id(correlation_id),
    }

    seats_list = None
    """
    [
        {
            "id": 1,
            "event": 1,
            "row": "A",
            "seat_number": "1",
            "price": "750.00",
            "status": "AVAILABLE",
            "held_until": None,
            "held_by_transaction_id": None,
            "user_id": None,
            "user_email": None,
        },
        {
            "id": 2,
            "event": 1,
            "row": "A",
            "seat_number": "2",
            "price": "750.00",
            "status": "AVAILABLE",
            "held_until": None,
            "held_by_transaction_id": None,
            "user_id": None,
            "user_email": None,
        }
    ]
    """

    try:
        resp = _http_post(
            INVENTORY_PRICING_URL,
            json={"seat_ids": seat_ids},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            seats_list = resp.json() # Deserializer : Converts JSON string -> Python object (list of dicts)
    except Exception as e:
        return None, None, None, Response(
            {
                "error": "Could not connect to Inventory Service to verify seat pricing.",
                "code": "INVENTORY_UNAVAILABLE",
                "detail": str(e),
            },
            status=503,
        )

    if seats_list and isinstance(seats_list, list):

        if len(seats_list) == len(seat_ids):
            # Using single for loop to complete this process might be clean but that wont give us a drastic speed improvement,
            # but current code has good readability with each line has one single, independent responsibilty.
            # typical time to takes to loop over 4 seats or 5 seats is about 0.000003 seconds(3 microseconds).
            total_amount = sum(Decimal(str(s.get("price"))) for s in seats_list)

            resolved_seat_ids = [s["id"] for s in seats_list]

            resolved_seat_numbers = ", ".join(f"{s.get('row', '')}{s.get('seat_number', '')}" for s in seats_list)
            # EX: "A1, A2, B5"

            return total_amount, resolved_seat_ids, resolved_seat_numbers, None
        else:
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


def _request_payload_verify(request):
    """
    Validates that incoming transaction creation requests provide required business data.
    Enforces strict allowlist of client fields:
      - Only 'seat_ids' is permitted from clients.
      - Disallowed fields: 'transaction_id', 'amount', 'user_id', 'user_email',
        'seat_id', 'seat_numbers', 'quantity', or any unrecognized fields.
      - If any disallowed or extra fields are sent, returns HTTP 400 Bad Request prompting the client.
    """
    if not isinstance(request.data, dict):
        return None, None, Response({"error": "Request body must be a valid JSON object"}, status=400)

    from django.conf import settings

    is_dev = getattr(settings, "DEBUG", False)

    allowed_fields = {"seat_ids", "transaction_id", "fail_compensation", "coordinator"}

    invalid_fields = [k for k in request.data.keys() if k not in allowed_fields]

    if invalid_fields:
        return None, None, Response(
            {
                "error": f"Invalid field(s) in payload: {', '.join(sorted(invalid_fields))}. These fields cannot be supplied by the client.",
                "code": "INVALID_FIELDS",
                "invalid_fields": sorted(list(invalid_fields)),
            },
            status=400,
        )

    # Validate seat identifier is present
    raw_seats = request.data.get("seat_ids")

    if not raw_seats or not isinstance(raw_seats, list):
        return None, None, Response(
            {
                "error": "seat_ids is required and must be a non-empty list of integers.",
                "code": "SEATS_REQUIRED",
            },
            status=400,
        )

    if any(type(seat_id) is not int for seat_id in raw_seats):
        return None, None, Response(
            {
                "error": "seat_ids must be a list of integers.",
                "code": "INVALID_SEAT_IDS",
            },
            status=400,
        )

    seat_ids = list(raw_seats)


    return seat_ids, True, None # Satisfied (for Production)


def _handle_create_transaction(request):
    seat_ids, payload_legit, invalid = _request_payload_verify(request)
    if invalid:
        return invalid

    from django.conf import settings

    # DEBUG (True) : what source code (Django) does
    #   1. Gives rich yellow traceback screen (django.views.debug)
    #   Django calls its internal technical_500_response() function. It inspects the entire Python call stack and builds that famous yellow web page containing:
    #       - The exact file and line number that crashed
    #       - The values of every local variable at every step in the stack trace
    #       - All HTTP headers, cookies, and environment settings
    #   2. Recording every SQL query in RAM

    # IS_TESTING (True) : Only current project uses it
    #   1. Whenever test cases executed with (python manage.py test) this varaible becomes True because of 'test' in sys.argv statement.

    is_testing = bool(getattr(settings, "IS_TESTING", False))

    is_dev = getattr(settings, "DEBUG", False)

    # 1. Staff Key Check (Priority 1)
    # X-Staff-Key is used for machine-to-machine / admin / staff booking.
    # If the user sends a valid X-Staff-Key, they are immediately authorized.
    # JWT authentication is bypassed completely (no get_user_from_request, no token verification).
    staff_api_key = os.getenv("STAFF_API_KEY")
    has_staff_key = bool(
        staff_api_key
        and request.headers.get("X-Staff-Key")
        and secrets.compare_digest(request.headers.get("X-Staff-Key"), staff_api_key)
    )

    if has_staff_key:
        user_id = request.headers.get("X-User-ID") or os.getenv("STAFF_USER_ID") or "system_staff"
        user_email = request.headers.get("X-User-Email") or os.getenv("STAFF_EMAIL_ID") or "admin@dtc.internal"

    else:
        # 2. Regular User: Authenticate via JWT Bearer token
        user_info = get_user_from_request(request)

        if user_info and user_info.get("is_authenticated"):
            user_id = user_info.get("user_id")
            user_email = user_info.get("user_email")

        elif is_testing or is_dev:
            user_id = request.headers.get("X-User-ID") or None
            user_email = request.headers.get("X-User-Email") or None

        else:
            # In production (DEBUG=False): Reject unauthenticated requests!
            return Response(
                {
                    "error": "Authentication required. Your session has expired or you are not signed in. Please sign in to book seats.",
                    "code": "AUTHENTICATION_REQUIRED",
                },
                status=401,
            )

    # 2. Transaction ID: Use client-supplied UUID if provided (for race condition / idempotency testing) or generate UUIDv4
    client_tx_id = request.data.get("transaction_id") if isinstance(request.data, dict) else None
    if client_tx_id:
        try:
            uuid.UUID(str(client_tx_id))
            transaction_id = str(client_tx_id)
        except (ValueError, AttributeError):
            transaction_id = str(uuid.uuid4())
    else:
        transaction_id = str(uuid.uuid4())

    # 3. Resolve Seats and Server-Side Pricing from Database

    amount, seat_ids, seat_numbers, price_error = _resolve_seat_pricing(
        seat_ids = seat_ids,
        correlation_id = request.correlation_id
    )

    if price_error: # On Response(...)
        return price_error
    
    quantity = len(seat_ids)

    # Simulation flag for payment decline (Dev / Visualizer Demo)
    simulate_decline = (
        request.query_params.get("simulate_payment_decline") == "true"
        or request.headers.get("X-Simulate-Payment-Decline") == "true"
    )

    # Test flag
    if simulate_decline:
        amount = Decimal("9999")

    # Test flag
    fail_compensation = request.data.get("fail_compensation", False) if request.data else False
    correlation_id = request.correlation_id # attached by the middleware

    if not correlation_id:
        raise RuntimeError("CorrelationIdMiddleware is missing or misconfigured.")

    created, dup_response = _create_transaction(
        transaction_id=transaction_id,
        seat_ids=seat_ids,
        seat_numbers=seat_numbers,
        amount=amount,
        quantity=quantity,
        user_id=user_id,
        user_email=user_email,
    )
    if not created:
        return dup_response

    # Simulation flag for coordinator crash 
    if request.data.get("coordinator"):
        return Response(
            {
                "transaction_id": str(transaction_id),
                "user_id": user_id,
                "user_email": user_email,
                "status": "STARTED",
                "error": "Coordinator process crashed mid-flight (simulated)",
                "orphaned": True,
            },
            status=500,
        )

    # If needed sync response
    is_sync = request.query_params.get("sync") == "true"
    
    if not is_sync:
        # Try async via Celery (supervisord keeps worker alive alongside gunicorn)
        try:
            from .tasks import execute_saga_task

            """
            CELERY '.delay(...)' vs DIRECT IN-THREAD EXECUTION: ARCHITECTURAL BREAKDOWN

            1. WHAT '.delay(...)' DOES:
               - Shortcut for 'execute_saga_task.apply_async(args=..., kwargs=...)'.
               - Instead of running Python code immediately, Celery packages the task name and 
                 parameters into a JSON message and pushes it to Redis (CELERY_BROKER_URL).
               - Returns immediately in ~1-2ms without waiting for downstream microservices(Inventory, Payment, ..).
               - The web server responds instantly with HTTP 202 Accepted + poll_url, freeing 
                 Gunicorn/Django web workers to handle thousands of concurrent incoming requests.
               - An independent background Celery worker daemon ('celery -A coordinator_service worker')
                 pops the task from Redis, executes the full Saga pipeline (Inventory -> Payment -> Confirm),
                 and records the final state (COMPLETED / COMPENSATED) in PostgreSQL.
               - The frontend/client polls 'GET /transactions/<id>/' until the background task finishes.

            2. WHAT HAPPENS WITHOUT '.delay(...)' (Direct function call):
               - Bypasses Celery, Redis, and background workers entirely.
               - Runs synchronously inside the active web server process/thread that handled the HTTP request.
               - THREAD BLOCKING: The Gunicorn worker freezes waiting for 3 consecutive network hops:
                   * Reserve seats (/seats/reserve/):  ~50ms
                   * Charge payment (/payments/charge/): ~200ms
                   * Confirm seats (/seats/confirm/):   ~50ms
                   Total latency: 300ms - 1500ms+ instead of 2ms.
               - WORKER STARVATION: In production with 4 web workers, capacity drops from ~2000 req/s 
                 down to ~8 req/s. Concurrent users quickly get connection drops or 504 Gateway Timeouts.
               - CLIENT TIMEOUTS: If Payment or Inventory has a slow network spike, browser/Nginx 
                 timeouts sever the connection before completion, risking orphaned states.
            """
            execute_saga_task.delay(
                str(transaction_id),
                seat_ids=seat_ids,
                amount=amount,
                fail_compensation=fail_compensation,
                correlation_id=correlation_id,
                user_id=user_id,
                user_email=user_email,
            )
            return Response(
                {
                    "transaction_id": str(transaction_id),
                    "user_id": user_id,
                    "user_email": user_email,
                    "status": "PENDING",
                    "poll_url": f"/transactions/{transaction_id}/",
                    "stream_url": f"/transactions/{transaction_id}/stream/",
                    "message": "Saga dispatched to Celery worker",
                },
                status=202,
                headers={"Location": f"/transactions/{transaction_id}/"},
            )
        except Exception:
            import logging

            logger = logging.getLogger(__name__)
            
            logger.exception("Failed to dispatch Saga task")
            return Response(
                {
                    "transaction_id": str(transaction_id),
                    "status": Transaction.Status.STARTED,
                    "error": "Saga could not be dispatched",
                },
                status=503,
            )

    # Synchronous execution: when sync=true is requested (e.g. tests or direct API calls)
    return _run_saga_pipeline(
        transaction_id=transaction_id,
        seat_ids=seat_ids,
        amount=amount,
        fail_compensation=fail_compensation,
        correlation_id=correlation_id,
        actor=SagaEvent.ACTOR_REQUEST,
        user_id=user_id,
        user_email=user_email,
    )


def _handle_list_transactions(request):
    is_authenticated, auth_user_id, is_staff = _get_requester_identity(request)
    if not is_authenticated:
        return Response({"error": "Authentication required."}, status=401)

    # .GET.get for query params
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

    # No database call yet
    queryset = Transaction.objects.all().order_by("-created_at") # Hypen means descending order

    # IDOR protection: Non-staff users only see their own transactions
    if not is_staff:
        if auth_user_id:
            queryset = queryset.filter(user_id=auth_user_id)
        else:
            return Response(
                {
                    "count": 0,
                    "limit": limit,
                    "offset": offset,
                    "results": [],
                }
            )

    if status_filter:
        queryset = queryset.filter(status=status_filter)

    total_count = queryset.count()
    txs = queryset[offset : offset + limit]

    # When Python reaches the list comprehension
    # Before the loop starts:
    # Django executes ONE single SQL query to get all 10 rows in a single batch and store it inside the `txs`.
    data = [_serialize_transaction(t) for t in txs]

    return Response(
        {
            "count": total_count,
            "limit": limit,
            "offset": offset,
            "results": data,
        }
    )


# ==============================================================================
# SECTION 4: SAGA ORCHESTRATOR PIPELINE
# Coordinates the distributed transaction workflow:
# 1. Reserve Inventory -> 2. Charge Payment -> 3. Confirm Inventory or Compensate
# ==============================================================================

def _run_saga_pipeline(
    transaction_id,
    seat_ids=None,
    amount=None,
    fail_compensation=False,
    correlation_id=None,
    actor=SagaEvent.ACTOR_REQUEST,

    user_id=None,
    user_email=None,
):


    response, reserve_error = _reserve_inventory(
        transaction_id,
        seat_ids,
        correlation_id=correlation_id,
        user_id=user_id,
        user_email=user_email,
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

    # If reponse is 200:
    resp_json = None

    try:
        resp_json = response.json()
        """
        Ex:
        {
          "message": "1 Seats reserved",
          "seat_ids": [
            55
          ],
          "held_until": "2026-09-02T07:21:32.111744Z",
          "total_amount": "350.00",
          "transaction_id": "e6420fe2-144c-4d17-88d8-e1528f227d8c"
        }
        """
    except Exception:
        pass

    if resp_json and "total_amount" in resp_json:
        try:
            from decimal import Decimal

            actual_amount = Decimal(str(resp_json["total_amount"]))
            # Preserve intentional payment failure simulation amount (9999) for compensation scenarios
            if str(amount).split(".")[0] != "9999":
                amount = actual_amount
                txn = Transaction.objects.filter(transaction_id=transaction_id).first()
                if txn:
                    txn.amount = amount
                    txn.save(update_fields=["amount"])
        except (ValueError, TypeError):
            pass

    _set_status_with_event(
        transaction_id,
        Transaction.Status.INVENTORY_RESERVED, # to
        expected_status=Transaction.Status.STARTED, # from
        actor=actor,
        http_status_code=response.status_code,
        http_payload=resp_json,
    )
    _set_status_with_event(
        transaction_id,
        Transaction.Status.PAYMENT_PROCESSING, # to 
        expected_status=Transaction.Status.INVENTORY_RESERVED, # from
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
        user_id=user_id,
        user_email=user_email,
    )


# ==============================================================================
# SECTION 5: SAGA MICROSERVICE REMOTE CALLS
# Individual remote HTTP interactions with Inventory and Payment microservices.
# ==============================================================================

def _reserve_inventory(transaction_id, seat_ids, correlation_id=None, user_id=None, user_email=None):

    headers = {
        "X-Correlation-ID": correlation_id,
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
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


def _charge_payment(
    transaction_id,
    amount,
    fail_compensation,
    correlation_id=None,
    seat_ids=None,
    user_id=None,
    user_email=None,
):

    headers = {
        "X-Correlation-ID": correlation_id,
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
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
    except (Timeout, ConnectionError) as e:
        uncertain_status = _transport_status(
            e, Transaction.Status.PAYMENT_TIMEOUT, Transaction.Status.PAYMENT_CONNECTION_ERROR
        )
    
        _set_status_with_event(
            transaction_id,
            uncertain_status, # to
            expected_status=Transaction.Status.PAYMENT_PROCESSING, # from
            actor=SagaEvent.ACTOR_REQUEST,
            http_status_code=None,
            http_payload=None,
        )

        try:
            payment_check = _http_get(
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


def _confirm_inventory(transaction_id, seat_ids=None, correlation_id=None):
    if not seat_ids:
        txn = Transaction.objects.filter(transaction_id=transaction_id).first()
        if txn and txn.seat_ids:
            seat_ids = txn.seat_ids
    headers = {
        "X-Correlation-ID": correlation_id,
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
    }
    try:
        response = _http_post(
            INVENTORY_CONFIRM_URL,
            json={"seat_ids": seat_ids, "transaction_id": str(transaction_id)},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        return response, None
    except (Timeout, ConnectionError) as e:
        return None, e


def _finalize_payment_outcome(transaction_id, fail_compensation, payment_data, payment_status_code=200, correlation_id=None, seat_ids=None):
    if payment_data.get("status") == "CHARGED":
        confirm_resp, confirm_err = _confirm_inventory(transaction_id, seat_ids=seat_ids, correlation_id=correlation_id)

        if confirm_resp and confirm_resp.status_code == 200:
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
            txn = Transaction.objects.filter(transaction_id=transaction_id).first()
            data = _serialize_transaction(txn) if txn else {
                "transaction_id": str(transaction_id),
                "status": Transaction.Status.COMPLETED,
            }
            data.update({
                "payment_response": payment_data,
                "inventory_response": confirm_json,
            })
            return Response(data)

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


def _compensate(transaction_id, fail_compensation, payment_data, payment_status_code=None, correlation_id=None, seat_ids=None):
    release_transaction_id = "fail-release" if fail_compensation else str(transaction_id)
    headers = {
        "X-Correlation-ID": _get_active_correlation_id(correlation_id),
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
    }

    _set_status_with_event(
        transaction_id,
        Transaction.Status.COMPENSATING, # to
        expected_status=( # from
            Transaction.Status.PAYMENT_PROCESSING,
            Transaction.Status.PAYMENT_TIMEOUT,
            Transaction.Status.PAYMENT_CONNECTION_ERROR,
        ),
        actor=SagaEvent.ACTOR_REQUEST,
        http_status_code=payment_status_code,
        http_payload=payment_data,
    )

    # Check if payment was charged and needs a refund
    was_charged = False
    if isinstance(payment_data, dict):
        if payment_data.get("status") == "CHARGED":
            was_charged = True
        elif isinstance(payment_data.get("payment"), dict) and payment_data["payment"].get("status") == "CHARGED":
            was_charged = True
    elif payment_status_code == 200:
        was_charged = True

    # 1. Attempt Inventory Release
    release_ok = False
    release_data = None
    release_status_code = None
    release_transport_error = None
    try:
        release_response = _http_post(
            INVENTORY_RELEASE_URL,
            json={"transaction_id": release_transaction_id, "seat_ids": seat_ids},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        release_status_code = release_response.status_code
        release_ok = (release_status_code == 200)
        try:
            release_data = release_response.json()
        except Exception:
            pass
    except (Timeout, ConnectionError) as e:
        release_transport_error = e

    # 2. Attempt Payment Refund if money was charged (independent of inventory release outcome)
    refund_ok = not was_charged
    refund_data = None
    refund_status_code = None
    refund_transport_error = None
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
            try:
                refund_data = refund_response.json()
            except Exception:
                pass
        except (Timeout, ConnectionError) as e:
            refund_transport_error = e

    # 3. Resolve Overall Compensation State
    if release_ok and refund_ok:
        _set_status_with_event(
            transaction_id,
            Transaction.Status.COMPENSATED,
            expected_status=Transaction.Status.COMPENSATING,
            actor=SagaEvent.ACTOR_REQUEST,
            http_status_code=release_status_code or 200,
            http_payload=release_data,
        )
        txn = Transaction.objects.filter(transaction_id=transaction_id).first()
        data = _serialize_transaction(txn) if txn else {
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

    # Partial or full failure during compensation
    _set_status_with_event(
        transaction_id,
        Transaction.Status.COMPENSATION_FAILED,
        expected_status=Transaction.Status.COMPENSATING,
        actor=SagaEvent.ACTOR_REQUEST,
        http_status_code=refund_status_code if not refund_ok else release_status_code,
        http_payload=refund_data if not refund_ok else release_data,
    )
    txn = Transaction.objects.filter(transaction_id=transaction_id).first()
    data = _serialize_transaction(txn) if txn else {
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
        cause = _transport_status(active_err, Transaction.Status.TIMEOUT, Transaction.Status.CONNECTION_FAILED)
        data["cause"] = cause
        data["error"] = "Network error during compensation"
        return Response(data, status=503)

    data["error"] = "Compensation failed"
    return Response(data, status=500)


# ==============================================================================
# SECTION 6: SERIALIZATION, IDENTITY & INFRASTRUCTURE HELPERS
# Utility functions for auth identity extraction, database creation, serialization,
# and exception-to-status mapping.
# ==============================================================================

def _get_requester_identity(request):
    """
    Resolves caller identity and permissions (is_authenticated, auth_user_id, is_staff).
    
    Priority of Authentication:
      1. JWT Bearer Token (Primary mechanism for Visualizer & API clients)
      2. Service API Key (Machine-to-Machine / Admin scripts via X-Staff-Key)
      3. Automated Test Runner Environment (Preserves test suite execution)
      4. Local Development Fallback (DEBUG=True demo user)
    """
    from django.conf import settings

    # Machine-to-Machine: Internal Staff Secret Key (Priority 1)
    staff_key = os.getenv("STAFF_API_KEY")
    
    if staff_key and request.headers.get("X-Staff-Key") and secrets.compare_digest(request.headers.get("X-Staff-Key"), staff_key):

        return True, "system_staff", True

    # JWT Bearer Token
    user_info = get_user_from_request(request)
    admin_domain = os.getenv("ADMIN_DOMAIN")
    
    if user_info and user_info.get("is_authenticated"):
        user_email = user_info.get("user_email")
        is_staff = (user_email and user_email.endswith(admin_domain))

        return True, user_info.get("user_id"), is_staff

    # Automated Test Runner Environment
    is_testing = bool(getattr(settings, "IS_TESTING", False))

    if is_testing:
        test_uid = request.headers.get("X-User-ID") or "system_test"

        return True, test_uid, True

    # Local Development Mode (DEBUG=True)
    is_dev = getattr(settings, "DEBUG", False)

    if is_dev:
        dev_uid = (
            request.headers.get("X-User-ID")
            or (user_info.get("user_id") if user_info else "system_dev")
        )

        return True, dev_uid, True

    # Default: Unauthenticated
    return False, None, False


def _create_transaction(
    transaction_id, seat_ids=None, seat_numbers=None, amount=500, quantity=1, user_id=None, user_email=None
):

    try:
        with transaction.atomic(): # Roll-back when error raised
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
        # This block only runs when the transaction id server created conflict with existing record's id
        # but Collisions are mathematically impossible (2^122 possibilities)
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


_create_transaction_idempotent = _create_transaction


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
        "status": txn.status,
        "is_terminal": txn.status in terminal_statuses,
        "seat_ids": txn.seat_ids,
        "seat_numbers": txn.seat_numbers,
        "quantity": txn.quantity,
        "amount": str(txn.amount),
        "created_at": txn.created_at,
        "updated_at": txn.updated_at,
    }


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


def _transport_status(exc, timeout_status, conn_status):
    return timeout_status if isinstance(exc, Timeout) else conn_status


def _parse_json_response(response):
    try:
        return response.json()
    except ValueError:
        return None