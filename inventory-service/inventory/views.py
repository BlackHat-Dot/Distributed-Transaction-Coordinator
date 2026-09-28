import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import timedelta
from django.conf import settings
from django.db import transaction, connection
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

import redis

from .models import Event, Seat, Reservation
from .serializers import EventSerializer, SeatSerializer


def get_redis_client():
    broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
    try:
        return redis.Redis.from_url(
            broker_url, socket_connect_timeout=0.05, socket_timeout=0.05
        )
    except Exception:
        return None


@api_view(["GET"])
def health_check(request):
    return Response({"status": "healthy", "service": "inventory"})

@api_view(["GET"])
def event_list(request):
    """
    2. prefetch_related (Separate Queries — Joined in Python)
    You cannot use select_related for one-to-many or many-to-many relationships 
    because a SQL JOIN would duplicate the parent record hundreds of times for every child row.

    Instead, prefetch_related executes separate queries and links them in Python memory.
    """
    events = Event.objects.all().prefetch_related("seats")
    serializer = EventSerializer(events, many=True)

    return Response(serializer.data)

def reclaim_expired_holds():
    """
    Reclaims seats that are expired by time (held_until < now) OR abandoned by user
    (user exited browser/website, causing their Redis heartbeat session dtc:session:{email} to expire).
    """
    now = timezone.now()

    held_seats = list(Seat.objects.filter(status=Seat.Status.HELD))
    if not held_seats:
        return 0, 0

    expired_ids = set()
    redis_client = get_redis_client()

    for seat in held_seats:
        # 1. Time-based expiry (10m hold elapsed)
        if seat.held_until and seat.held_until < now:
            expired_ids.add(seat.id)
            continue

        # 2. Presence-based expiry (dead-man's switch: user exited browser / logged out)
        if seat.user_email and redis_client:
            try:
                if not redis_client.exists(f"dtc:session:{seat.user_email}"):
                    expired_ids.add(seat.id)
            except Exception:
                pass

    if not expired_ids:
        return 0, 0

    with transaction.atomic():
        released = Reservation.objects.filter(
            seat_id__in=expired_ids,
            status=Reservation.Status.RESERVED,
        ).update(status=Reservation.Status.RELEASED) # reclaimer

        reclaimed = Seat.objects.filter(id__in=expired_ids).update(
            status=Seat.Status.AVAILABLE,
            held_until=None,
            held_by_transaction_id=None,
            user_id=None,
            user_email=None,
        )

    return reclaimed, released


@api_view(["GET"])
def seat_list(request):
    # PERF-01: Removed reclaim_expired_holds() piggyback sweep from read path.
    # Expired holds are presented as AVAILABLE lazily by SeatSerializer,
    # and physically reclaimed asynchronously by Celery Beat / recovery sweeps.

    # PERF-02: Seed demo seats once per process in DEBUG mode if table is empty
    if getattr(settings, "DEBUG", False) and not getattr(settings, "_SEATS_SEEDED", False):
        if Seat.objects.count() == 0:
            event = Event.objects.first()

            if not event:
                event = Event.objects.create(
                    name='Coldplay: Music of the Spheres World Tour',
                    venue='Grand Arena Stadium',
                    starts_at=timezone.now() + timedelta(days=14)
                )

            seats_to_create = []

            for r, price in [('A', 750.00), ('B', 500.00), ('C', 350.00)]:
                for num in range(1, 11):
                    seats_to_create.append(
                        Seat(event=event, row=r, seat_number=str(num), price=price, status=Seat.Status.AVAILABLE)
                    )

            Seat.objects.bulk_create(seats_to_create)
        settings._SEATS_SEEDED = True

    seats = Seat.objects.all().order_by("id")
    serializer = SeatSerializer(seats, many=True)
    return Response(serializer.data)


INTERNAL_SERVICE_KEY = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY")
STAFF_API_KEY = os.getenv("STAFF_API_KEY")


def _is_staff_request(request):
    """
    Verifies staff/admin identity using constant-time comparison against STAFF_API_KEY.
    In production (DEBUG=False, IS_TESTING=False), it strictly fails closed if
    STAFF_API_KEY is not configured in the environment.
    """
    staff_key = request.headers.get("X-Staff-Key")
    if not staff_key:
        return False
    if STAFF_API_KEY:
        return secrets.compare_digest(staff_key, STAFF_API_KEY)
    if settings.DEBUG or getattr(settings, "IS_TESTING", False):
        return secrets.compare_digest(staff_key, "dtc-staff-secret-key-2026")
    return False


def _get_authenticated_user_id(request):
    """
    Extracts and cryptographically verifies the JWT from Authorization header.
    Returns user_id string if signature and expiration are valid, None otherwise.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header.split(" ")[1].strip()
    parts = token.split(".")
    if len(parts) != 3:
        return None

    h_str, p_str, sig_str = parts
    signing_input = f"{h_str}.{p_str}".encode("utf-8")
    secret = settings.SECRET_KEY.encode("utf-8")
    expected_sig = hmac.new(secret, signing_input, hashlib.sha256).digest()

    rem = len(sig_str) % 4
    if rem > 0:
        sig_str += "=" * (4 - rem)

    try:
        actual_sig = base64.urlsafe_b64decode(sig_str.encode("utf-8"))
        if not hmac.compare_digest(actual_sig, expected_sig):
            return None

        p_rem = len(p_str) % 4
        if p_rem > 0:
            p_str += "=" * (4 - p_rem)
        payload = json.loads(base64.urlsafe_b64decode(p_str.encode("utf-8")).decode("utf-8"))

        if "exp" in payload and payload["exp"] < int(time.time()):
            return None

        return str(payload.get("user_id")) if payload.get("user_id") else None
    except Exception:
        return None


def _verify_internal_service_key(request, allow_client=False):
    """
    Ensures internal state-mutating endpoints (confirm, release-booked)
    can only be called by internal services (e.g. coordinator) presenting
    the shared secret.
    If allow_client=True (e.g. reserve_seat or user hold cancel), calls without
    the header are permitted, but invalid keys are always rejected with 403.
    Returns (auth_err, is_internal).
    """
    key = request.headers.get("X-Internal-Service-Key")
    if key is not None:
        if not secrets.compare_digest(key, INTERNAL_SERVICE_KEY or ""):
            return Response(
                {"error": "Forbidden: invalid internal service key."},
                status=403,
            ), False
        return None, True

    if getattr(settings, "IS_TESTING", False):
        return None, True

    if allow_client:
        return None, False

    return Response(
        {"error": "Forbidden: missing internal service key."},
        status=403,
    ), False


@api_view(["POST"])
def seat_pricing(request):
    """
    Returns seat objects for the requested seat_ids.
    Accepts: POST {"seat_ids": [1, 2, 3]}
    Returns: JSON array of matching Seat objects.
    Called exclusively by Coordinator to resolve server-side pricing before booking.
    """
    auth_err, _ = _verify_internal_service_key(request)
    if auth_err:
        return auth_err

    seat_ids = request.data.get("seat_ids")

    if not seat_ids or not isinstance(seat_ids, list):
        return Response({"error": "seat_ids is required and must be a list"}, status=400)

    # defensive check for seat_ids type, coordinator already checked this and that is why the request reached here,
    # though it feels redudant, it protect the staff/admin from sending invalid seat_ids (because they have service key to reach the endpoint manually),
    # and this process wont take much time (approx. 300 ns ~  0.0003 ms)
    try:
        target_ids = list({int(x) for x in seat_ids})
    except (ValueError, TypeError):
        return Response({"error": "seaSeatSerializert_ids must be a list of integers"}, status=400)

    seats = Seat.objects.filter(id__in=target_ids).order_by("id")

    # Nothing yet started (Data query hasn't run yet, no loop has run), to_representation() has not been called, No JSON has been created).
    # but because many=True DRF wraps SeatSerializer inside a ListSerializer,
    # it save the variable pointer: self.instance = seats,
    # it sits idle, waiting for instructions.
    serializer = SeatSerializer(seats, many=True) # Think this line as `Gun`.

    # .data is a Python @property:

    """
    # Simplified DRF source code:
    @property
    def data(self):
       if not hasattr(self, '_data'):
           # 1. Triggers to_representation on your instance
           self._data = self.to_representation(self.instance)
       
       # 2. Caches and returns the clean dictionary/list
       return self._data
    """
    return Response(serializer.data) # .data ; the actual `trigger`.


def _validate_uuid(val):
    try:
        return uuid.UUID(str(val))
    except (ValueError, AttributeError):
        return None

def _validate_reserve_input(data):
    if not data:
        return None, None, Response({"message": "no input values"}, status=400)

    seat_ids = data.get("seat_ids")
    if not seat_ids:
        return None, None, Response({"error": "seat_ids is required"}, status=400)

    if not isinstance(seat_ids, (list, tuple)):
        return None, None, Response({"error": "seat_ids must be a list of integers"}, status=400)

    try:
        seat_ids = [int(s) for s in seat_ids]
    except (ValueError, TypeError):
        return None, None, Response({"error": "seat_ids must be a list of integers"}, status=400)

    raw_tid = data.get("transaction_id")
    if raw_tid:
        tid = _validate_uuid(raw_tid)
        if not tid:
            return None, None, Response({"error": "transaction_id must be a valid UUID"}, status=400)
    else:
        tid = None  # Caller will decide: generate server-side or reject

    return seat_ids, tid, None


@api_view(["POST"])
def reserve_seat(request):
    auth_err, has_internal_key = _verify_internal_service_key(request, allow_client=True) # Service key checker
    if auth_err:
        return auth_err

    seat_ids, supplied_tid, error_response = _validate_reserve_input(request.data)

    if error_response:
        return error_response

    # Determine the transaction_id:
    #   - Coordinator calls (X-Internal-Service-Key present) send their saga UUID in the body → honour it.
    #   - Browser calls never supply a valid transaction_id from the body (client-sent ID is silently ignored).

    try:
        if has_internal_key:
            if not supplied_tid:
                return Response({"error": "transaction_id is required"}, status=400)
            transaction_id = supplied_tid
        else:
            transaction_id = uuid.uuid4()  # Always freshly generated for browser callers
    except (ValueError):
        transaction_id = uuid.uuid4()

    # Read identity from request headers first (injected by fetchJson on every browser call),
    # falling back to body fields for coordinator / internal service calls.
    user_id = (
        request.headers.get("X-User-ID")
        or request.data.get("user_id")
        or None
    )
    user_email = (
        request.headers.get("X-User-Email")
        or request.data.get("user_email")
        or None
    )

    is_testing = bool(getattr(settings, "IS_TESTING", False))
    is_dev = getattr(settings, "DEBUG", False)

    if not (is_dev or is_testing or has_internal_key):
        if not user_id:
            return Response(
                {
                    "error": "Authentication required. Your session has expired or you are not signed in. Please sign in to hold seats.",
                    "code": "AUTHENTICATION_REQUIRED",
                },
                status=401,
            )
    # Enforce MAX_USER_HOLDS (D-5)
    max_user_holds = getattr(settings, "MAX_USER_HOLDS")
    
    if len(seat_ids) > max_user_holds:
        return Response(
            {
                "error": f"Requested seats count ({len(seat_ids)}) exceeds maximum allowed active holds ({max_user_holds}).",
                "code": "MAX_HOLDS_EXCEEDED",
                "active_holds": 0,
                "max_holds": max_user_holds,
                "transaction_id": str(transaction_id),
            },
            status=429,
        )

    try:
        with transaction.atomic():
            # CONC-01: Acquire transaction-scoped advisory lock on user_id to serialize hold checks per user
            if user_id and connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"user_hold:{user_id}"])

            seats = list(Seat.objects.select_for_update().filter(id__in=seat_ids).order_by('id'))

            if len(seats) != len(seat_ids):
                return Response({"error": "One or more seats not found", "transaction_id": str(transaction_id)}, status=404)

            now = timezone.now()


            if user_id:
                # Exclude objects which is held by current transaction id.
                active_holds = Seat.objects.filter(
                    user_id=user_id,
                    status=Seat.Status.HELD,
                    held_until__gt=now,
                ).exclude(held_by_transaction_id=transaction_id).count()

                # without .exclude(): If a user holds three seat then a network hicup happens then they retry,
                # active_holds become three plus len(seats) => 3 + 3 = 6 > 5(max hold) and User is blocked from booking, In fact the user only retried
                # but system blocked them due to a flaw.

                # with .exclude(): If a user holds three seat then a network hicup happens then they retry,
                # active_holds become three plus len(seats) => 0 + 3 = 3 < 5(max hold) and User is allowed to book

                # .exclude() is used especially used for this case to neglect previous same transaction that isn't yet completed and that's been retried.
                if active_holds + len(seats) > max_user_holds:
                    return Response(
                        {
                            "error": f"User exceeds maximum allowed active holds ({max_user_holds}).",
                            "code": "MAX_HOLDS_EXCEEDED",
                            "active_holds": active_holds,
                            "max_holds": max_user_holds,
                            "transaction_id": str(transaction_id),
                        },
                        status=429,
                    )
            
            for seat in seats:
                # 1. Is it already held by this transaction? (Idempotent retry)
                if seat.held_by_transaction_id and str(seat.held_by_transaction_id) == str(transaction_id):
                    continue
                
                # 2. Is it held by the same user? (Transition temporary hold to checkout)
                if seat.is_held and not seat.is_expired(now) and user_id and seat.user_id == user_id:
                    continue
                
                # 3. Is it available for anyone to take? (Available or expired)
                if seat.is_available or (seat.is_held and seat.is_expired(now)):
                    continue
                
                # Otherwise, someone else owns it or it's already booked!
                return Response(
                    {
                        "error": f"Seat {seat.row}{seat.seat_number} is already booked or held",
                        "code": "SEAT_UNAVAILABLE",
                        "transaction_id": str(transaction_id),
                    },
                    status=409,
                )

            held_until_dt = now + timedelta(minutes=10)
            
            reservations = []
            for seat in seats:
                # Clean up any unfinalized hold on this seat so it doesn't get orphaned
                if seat.held_by_transaction_id and str(seat.held_by_transaction_id) != str(transaction_id):
                    Reservation.objects.filter(
                        seat=seat,
                        transaction_id=seat.held_by_transaction_id,
                        status=Reservation.Status.RESERVED,
                    ).delete()

                seat.status = Seat.Status.HELD
                seat.held_until = held_until_dt
                seat.held_by_transaction_id = transaction_id
                seat.user_id = user_id
                seat.user_email = user_email
                seat.save()
                
                res, created = Reservation.objects.get_or_create(
                    seat=seat, # used lookup
                    transaction_id=transaction_id, # used lookup
                    defaults={'user_id': user_id, 'user_email': user_email, 'status': Reservation.Status.RESERVED} # used only if the object doesn't exist
                )
                # object created using those lookup + defaults
                # .get_or_create() gives `object, True` if object is created if not then `object, False`

                if not created:
                    res.status = Reservation.Status.RESERVED
                    res.save()
                    
                reservations.append(res)
            
            total_amount = sum(seat.price for seat in seats)
            
            resp_data = {
                "message": f"{len(seats)} Seats reserved",
                "transaction_id": str(transaction_id),
                "seat_ids": seat_ids,
                "held_until": held_until_dt,
                "total_amount": str(total_amount),
            }
            return Response(resp_data, status=200)

    except Exception as e:
        return Response({"error": "Database error", "detail": str(e)}, status=500)

@api_view(["POST"])
def confirm_seat(request):
    auth_err, _ = _verify_internal_service_key(request)
    if auth_err:
        return auth_err

    seat_ids, transaction_id, error_response = _validate_reserve_input(request.data)
    if error_response:
        return error_response
    if not transaction_id:
        return Response({"error": "transaction_id is required"}, status=400)


    try:
        with transaction.atomic():
            reservations = list(Reservation.objects.select_for_update().filter(transaction_id=transaction_id))

            if not reservations:
                return Response({"error": "Reservations not found", "transaction_id": str(transaction_id)}, status=404)

            seats = list(Seat.objects.select_for_update().filter(id__in=seat_ids).order_by('id'))

            if len(seats) != len(seat_ids):
                return Response({"error": "One or more seats not found", "transaction_id": str(transaction_id)}, status=404)

            now = timezone.now()

            for seat in seats:
                # This condition checks whether this seat object is booked by this transaction id
                if seat.status == Seat.Status.BOOKED and str(seat.held_by_transaction_id) == str(transaction_id):
                    continue

                # This condition checks whether this seat object is held by this transaction id
                if str(seat.held_by_transaction_id) != str(transaction_id):
                    return Response(
                        {
                            "error": f"Seat {seat.row}{seat.seat_number} hold does not belong to transaction {transaction_id}",
                            "code": "HOLD_EXPIRED",
                            "status": "HOLD_EXPIRED",
                            "transaction_id": str(transaction_id),
                        },
                        status=409,
                    )

                if seat.held_until and seat.held_until < now and seat.status != Seat.Status.BOOKED:
                    return Response(
                        {
                            "error": f"Seat {seat.row}{seat.seat_number} hold has expired before confirmation",
                            "code": "HOLD_EXPIRED",
                            "status": "HOLD_EXPIRED",
                            "transaction_id": str(transaction_id),
                        },
                        status=409,
                    )

            Reservation.objects.filter(transaction_id=transaction_id).update(
                status=Reservation.Status.BOOKED
            )

            Seat.objects.filter(id__in=[s.id for s in seats]).update(
                status=Seat.Status.BOOKED,
                held_until=None,
                held_by_transaction_id=transaction_id,
            )

            return Response({"message": "Seats confirmed", "transaction_id": str(transaction_id)}, status=200)
    except Exception as e:
        return Response({"error": "Database error", "detail": str(e)}, status=500)

@api_view(["POST"])
def release_seat(request):
    # 1. Staff key check (Full admin override)
    is_staff = _is_staff_request(request)

    # 2. Internal service key check (Coordinator Saga rollback)
    internal_key = request.headers.get("X-Internal-Service-Key")
    is_internal = bool(
        internal_key and INTERNAL_SERVICE_KEY and secrets.compare_digest(internal_key, INTERNAL_SERVICE_KEY)
    )

    # 3. User JWT verification (Must be authenticated if not staff/internal)
    auth_user_id = None
    if not (is_staff or is_internal):
        auth_user_id = _get_authenticated_user_id(request)
        if not auth_user_id and not getattr(settings, "IS_TESTING", False):
            return Response(
                {"error": "Authentication required. Please sign in to release a hold.", "code": "AUTH_REQUIRED"},
                status=401,
            )

    # 4. Strict payload rule: Reject seat_ids; require only transaction_id
    if isinstance(request.data, dict) and "seat_ids" in request.data:
        return Response(
            {"error": "seat_ids cannot be supplied. Release is performed by transaction_id only.", "code": "INVALID_FIELDS"},
            status=400,
        )

    raw_tid = request.data.get("transaction_id") if isinstance(request.data, dict) else None
    if not raw_tid:
        return Response({"error": "transaction_id is required", "code": "TRANSACTION_ID_REQUIRED"}, status=400)

    try:
        transaction_id = uuid.UUID(str(raw_tid))
    except (ValueError, TypeError):
        return Response({"error": "transaction_id must be a valid UUID", "code": "INVALID_UUID"}, status=400)

    try:
        with transaction.atomic():
            reservations = list(Reservation.objects.select_for_update().filter(transaction_id=transaction_id))

            if not reservations:
                return Response(
                    {"message": "No active reservation found to release.", "released_count": 0, "transaction_id": str(transaction_id)},
                    status=200,
                )

            active_res = []
            for r in reservations:
                # Fail fast 1: Ownership mismatch for regular users
                if auth_user_id and not (is_staff or is_internal):
                    if r.user_id and str(r.user_id) != str(auth_user_id):
                        return Response(
                            {"error": "Forbidden: You do not own this reservation hold.", "code": "HOLD_OWNERSHIP_MISMATCH"},
                            status=403,
                        )

                # Fail fast 2: Regular user or coordinator attempting to release confirmed/booked ticket
                if not is_staff and r.status == Reservation.Status.BOOKED:
                    return Response(
                        {
                            "error": "This reservation is already booked and confirmed. Users cannot cancel booked tickets. Please contact staff.",
                            "code": "CANNOT_RELEASE_BOOKED",
                            "transaction_id": str(transaction_id),
                        },
                        status=403,
                    )

                # Collect active reservations to release
                if is_staff:
                    if r.status in (Reservation.Status.RESERVED, Reservation.Status.BOOKED):
                        active_res.append(r)
                else:
                    if r.status == Reservation.Status.RESERVED:
                        active_res.append(r)

            # If nothing was eligible to release (e.g. hold was already released earlier or expired)
            if not active_res:
                return Response(
                    {
                        "message": "Nothing to release. Holds may have already been cancelled or expired.",
                        "released_count": 0,
                        "transaction_id": str(transaction_id),
                    },
                    status=200,
                )

            seat_ids = [r.seat_id for r in active_res if r.seat_id]
            Reservation.objects.filter(id__in=[r.id for r in active_res]).update(
                status=Reservation.Status.RELEASED
            )

            if seat_ids:
                Seat.objects.filter(
                    id__in=seat_ids,
                    held_by_transaction_id=transaction_id
                ).update(
                    status=Seat.Status.AVAILABLE,
                    held_until=None,
                    held_by_transaction_id=None,
                    user_id=None,
                    user_email=None,
                )

            return Response(
                {
                    "message": f"Successfully released {len(seat_ids)} seat(s).",
                    "released_count": len(seat_ids),
                    "transaction_id": str(transaction_id),
                },
                status=200,
            )

    except Exception as e:
        return Response({"error": "Database error", "detail": str(e)}, status=500)

@api_view(["GET"])
def check_reservation(request):
    raw_tid = request.query_params.get("transaction_id")
    if not raw_tid:
        return Response({"error": "transaction_id is required"}, status=400)
        
    try:
        import uuid

        transaction_id = uuid.UUID(raw_tid)
    except ValueError:
        return Response({"error": "transaction_id must be a valid UUID"}, status=400)

    reservations = list(Reservation.objects.filter(transaction_id=transaction_id))

    if not reservations:
        return Response({"status": Reservation.Status.NOT_RESERVED, "transaction_id": str(transaction_id)}, status=200)

    if any(r.status == Reservation.Status.BOOKED for r in reservations):
        return Response({"status": Reservation.Status.BOOKED, "transaction_id": str(transaction_id)}, status=200)
    
    if any(r.status == Reservation.Status.RELEASED for r in reservations):
        return Response({"status": Reservation.Status.RELEASED, "transaction_id": str(transaction_id)}, status=200)

    return Response({"status": Reservation.Status.RESERVED, "transaction_id": str(transaction_id)}, status=200)


@api_view(["POST"])
def reset_seats(request):
    if not settings.DEBUG:
        if not _is_staff_request(request):
            return Response(
                {"error": "Resetting venue seats is restricted."},
                status=403,
            )

    with transaction.atomic():
        Seat.objects.update(
            status=Seat.Status.AVAILABLE,
            held_until=None,
            held_by_transaction_id=None,
            user_id=None,
            user_email=None,
        )

        Reservation.objects.all().delete() # must

    return Response({"message": "All seats reset to AVAILABLE."})


@api_view(["POST"])
def sweep_expired_holds(request):
    """
    Sweeps expired and abandoned seat holds and marks them as AVAILABLE.
    Pure background management / maintenance endpoint.
    """
    reclaimed_count, _ = reclaim_expired_holds()
    return Response({
        "message": f"Swept {reclaimed_count} expired seat hold(s).",
        "reclaimed_count": reclaimed_count,
    }, status=200)

