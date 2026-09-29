from decimal import Decimal
import logging
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
from django.db import transaction, connection, IntegrityError, DatabaseError
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

import jwt
import redis

from .models import Event, Seat, Reservation
from .serializers import EventSerializer, SeatSerializer, PublicSeatSerializer, InternalSeatSerializer

logger = logging.getLogger(__name__)


_redis_client_instance = None


def get_redis_client():
    global _redis_client_instance
    if _redis_client_instance is not None:
        return _redis_client_instance

    redis_url = getattr(settings, "REDIS_URL", None) or getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
    try:
        client = redis.Redis.from_url(
            redis_url, socket_connect_timeout=0.2, socket_timeout=0.2, decode_responses=True
        )
        client.ping()
        _redis_client_instance = client
        return _redis_client_instance
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
    Presence-based reclaim only applies after a grace period of HOLD_PRESENCE_GRACE_SECONDS (default 120s).
    """
    now = timezone.now()

    held_seats = list(
        Seat.objects.filter(status=Seat.Status.HELD).values(
            "id", "held_until", "user_email", "held_by_transaction_id"
        )
    )
    if not held_seats:
        return 0, 0

    expired_snapshots = {}  # seat_id -> held_by_transaction_id
    redis_client = get_redis_client()

    grace_seconds = getattr(settings, "HOLD_PRESENCE_GRACE_SECONDS", 120)
    hold_duration_minutes = getattr(settings, "HOLD_DURATION_MINUTES", 10)

    presence_candidates = []

    for seat in held_seats:
        seat_id = seat["id"]
        # 1. Time-based expiry (10m hold elapsed) - stays unchanged
        if seat["held_until"] and seat["held_until"] < now:
            expired_snapshots[seat_id] = seat["held_by_transaction_id"]
            continue

        # 2. Candidate for presence-based expiry: only if hold age >= grace_seconds
        if seat["held_until"] and seat["user_email"] and redis_client:
            hold_start = seat["held_until"] - timedelta(minutes=hold_duration_minutes)
            hold_age = (now - hold_start).total_seconds()
            if hold_age >= grace_seconds:
                presence_candidates.append(seat)

    # Pipeline Redis EXISTS per unique email for presence candidates
    if presence_candidates and redis_client:
        unique_emails = list({s["user_email"] for s in presence_candidates if s.get("user_email")})
        active_emails = set()
        try:
            pipe = redis_client.pipeline()
            for email in unique_emails:
                pipe.exists(f"dtc:session:{email}")
            results = pipe.execute()
            if not isinstance(results, (list, tuple)) or len(results) != len(unique_emails):
                results = [redis_client.exists(f"dtc:session:{email}") for email in unique_emails]
            for email, exists in zip(unique_emails, results):
                if exists:
                    active_emails.add(email)

            for seat in presence_candidates:
                if seat["user_email"] not in active_emails:
                    expired_snapshots[seat["id"]] = seat["held_by_transaction_id"]
        except Exception:
            # If Redis errors, skip presence expiry (fail safe)
            pass

    if not expired_snapshots:
        return 0, 0

    with transaction.atomic():
        locked_seats = list(
            Seat.objects.select_for_update()
            .filter(id__in=list(expired_snapshots.keys()), status=Seat.Status.HELD)
            .order_by("id")
        )

        verified_reclaim_seats = []
        for seat in locked_seats:
            expected_tid = expired_snapshots.get(seat.id)
            # Compare held_by_transaction_id directly without truthiness
            if seat.held_by_transaction_id == expected_tid:
                verified_reclaim_seats.append(seat)

        if not verified_reclaim_seats:
            return 0, 0

        reclaimed_ids = [s.id for s in verified_reclaim_seats]
        pair_filter = Q()
        for s in verified_reclaim_seats:
            if s.held_by_transaction_id is not None:
                pair_filter |= Q(seat_id=s.id, transaction_id=s.held_by_transaction_id)
            else:
                pair_filter |= Q(seat_id=s.id)

        released = 0
        if pair_filter:
            released = Reservation.objects.filter(
                pair_filter,
                status=Reservation.Status.RESERVED,
            ).update(status=Reservation.Status.RELEASED)

        reclaimed = Seat.objects.filter(
            id__in=reclaimed_ids,
            status=Seat.Status.HELD,
        ).update(
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

            for r, price in [('A', Decimal("750.00")), ('B', Decimal("500.00")), ('C', Decimal("350.00"))]:
                for num in range(1, 11):
                    seats_to_create.append(
                        Seat(event=event, row=r, seat_number=str(num), price=price, status=Seat.Status.AVAILABLE)
                    )

            Seat.objects.bulk_create(seats_to_create)
        settings._SEATS_SEEDED = True

    seats = Seat.objects.all().order_by("id")
    serializer = PublicSeatSerializer(seats, many=True, context={"request": request})
    return Response(serializer.data)


def _is_staff_request(request):
    """
    Verifies staff/admin identity using constant-time comparison against settings.STAFF_API_KEY.
    """
    staff_key = request.headers.get("X-Staff-Key")
    if not staff_key:
        return False
    configured_key = getattr(settings, "STAFF_API_KEY", None)
    if configured_key:
        return secrets.compare_digest(str(staff_key), str(configured_key))
    return False



def _get_authenticated_user(request):
    """
    Extracts and cryptographically verifies the JWT from Authorization header.
    Returns dict {"user_id": ..., "user_email": ..., "sid": ...} if valid, else None.
    If header verification fails and ALLOW_UNVERIFIED_IDENTITY_HEADERS is True (e.g. in test env),
    falls back to unverified request headers.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        key = getattr(settings, "JWT_SIGNING_KEY", None) or settings.SECRET_KEY
        try:
            payload = jwt.decode(
                token,
                key,
                algorithms=["HS256"],
                options={"require": ["exp"]},
            )
            uid = payload.get("user_id")
            uemail = payload.get("user_email")
            if uid or uemail:
                return {
                    "user_id": str(uid) if uid is not None else None,
                    "user_email": str(uemail) if uemail is not None else None,
                    "sid": payload.get("sid"),
                }
        except jwt.PyJWTError as e:
            logger.debug("JWT verification failed: %s", type(e).__name__)
        except Exception as e:
            logger.debug("Unexpected error during JWT verification: %s", type(e).__name__)

    if getattr(settings, "ALLOW_UNVERIFIED_IDENTITY_HEADERS", False):
        uid = request.headers.get("X-User-ID")
        uemail = request.headers.get("X-User-Email")
        if uid or uemail:
            return {
                "user_id": str(uid) if uid else None,
                "user_email": str(uemail) if uemail else None,
                "sid": None,
            }

    return None



def _get_authenticated_user_id(request):
    """
    Extracts and cryptographically verifies the JWT from Authorization header.
    Returns user_id string if signature and expiration are valid, None otherwise.
    """
    auth_user = _get_authenticated_user(request)
    return auth_user.get("user_id") if auth_user else None


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
    configured_key = getattr(settings, "INTERNAL_SERVICE_KEY", None)

    if key is not None:
        if not configured_key or not secrets.compare_digest(str(key), str(configured_key)):
            return Response(
                {"error": "Forbidden: invalid internal service key."},
                status=403,
            ), False
        return None, True

    if getattr(settings, "IS_TESTING", False):
        auth_header = request.headers.get("Authorization", "")
        if allow_client and auth_header.startswith("Bearer "):
            return None, False
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
    serializer = InternalSeatSerializer(seats, many=True)

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

    # Finding 18: Dedupe seat_ids while preserving order, and cap at 50
    seat_ids = list(dict.fromkeys(seat_ids))
    if len(seat_ids) > 50:
        return None, None, Response({"error": "Cannot reserve more than 50 seats at once", "code": "MAX_SEATS_EXCEEDED"}, status=400)

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

    # Authenticate user:
    # 1. Cryptographically verify JWT from Authorization header
    # 2. If internal key is present (coordinator saga), trust user_id/user_email from body
    # 3. If ALLOW_UNVERIFIED_IDENTITY_HEADERS is True (e.g. testing), fall back to headers/body
    auth_user = _get_authenticated_user(request)

    if has_internal_key:
        user_id = request.data.get("user_id") or (auth_user.get("user_id") if auth_user else None)
        user_email = request.data.get("user_email") or (auth_user.get("user_email") if auth_user else None)
    elif auth_user:
        user_id = auth_user.get("user_id")
        user_email = auth_user.get("user_email")
    elif getattr(settings, "ALLOW_UNVERIFIED_IDENTITY_HEADERS", False) or getattr(settings, "IS_TESTING", False):
        user_id = request.headers.get("X-User-ID") or request.data.get("user_id") or None
        user_email = request.headers.get("X-User-Email") or request.data.get("user_email") or None
    else:
        user_id = None
        user_email = None

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
    max_user_holds = getattr(settings, "MAX_USER_HOLDS", None) or 10
    
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
            
            # If ANY requested seats are already BOOKED by this transaction:
            has_booked = any(s.status == Seat.Status.BOOKED and str(s.held_by_transaction_id) == str(transaction_id) for s in seats)
            if has_booked:
                # If ALL requested seats are BOOKED by this transaction, return idempotent success matching normal reserve shape
                if all(s.status == Seat.Status.BOOKED and str(s.held_by_transaction_id) == str(transaction_id) for s in seats):
                    total_amount = sum(s.price for s in seats)
                    return Response(
                        {
                            "message": f"{len(seats)} Seats reserved",
                            "transaction_id": str(transaction_id),
                            "seat_ids": [s.id for s in seats],
                            "held_until": None,
                            "total_amount": str(total_amount),
                        },
                        status=200,
                    )
                else:
                    return Response(
                        {
                            "error": "Conflict: Partial reservation mismatch. Some requested seats are already booked for this transaction.",
                            "code": "PARTIAL_SEATS_BOOKED_CONFLICT",
                            "transaction_id": str(transaction_id),
                        },
                        status=409,
                    )

            for seat in seats:
                # If a seat is BOOKED (and not handled by all-booked check above), it cannot be held
                if seat.status == Seat.Status.BOOKED:
                    return Response(
                        {
                            "error": f"Seat {seat.row}{seat.seat_number} is already booked",
                            "code": "SEAT_UNAVAILABLE",
                            "transaction_id": str(transaction_id),
                        },
                        status=409,
                    )

                # 1. Is it already held by this transaction? (Idempotent retry of active hold)
                if seat.status == Seat.Status.HELD and seat.held_by_transaction_id and str(seat.held_by_transaction_id) == str(transaction_id):
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
                seat.save(update_fields=["status", "held_until", "held_by_transaction_id", "user_id", "user_email"])

                res, created = Reservation.objects.get_or_create(
                    seat=seat, # used lookup
                    transaction_id=transaction_id, # used lookup
                    defaults={'user_id': user_id, 'user_email': user_email, 'status': Reservation.Status.RESERVED} # used only if the object doesn't exist
                )
                # object created using those lookup + defaults
                # .get_or_create() gives `object, True` if object is created if not then `object, False`

                if not created:
                    res.status = Reservation.Status.RESERVED
                    res.user_id = user_id
                    res.user_email = user_email
                    res.save(update_fields=["status", "user_id", "user_email"])

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

    except IntegrityError as e:
        logger.warning("IntegrityError in reserve_seat: %s", e)
        return Response(
            {
                "error": "Conflict: Seat reservation conflict or concurrent modification.",
                "code": "SEAT_CONFLICT",
                "transaction_id": str(transaction_id) if "transaction_id" in locals() and transaction_id else None,
            },
            status=409,
        )
    except DatabaseError as e:
        logger.exception("DatabaseError in reserve_seat: %s", e)
        return Response({"error": "Internal server error occurred.", "code": "DATABASE_ERROR"}, status=500)
    except Exception as e:
        logger.exception("Unexpected error in reserve_seat: %s", e)
        return Response({"error": "Internal server error occurred.", "code": "INTERNAL_ERROR"}, status=500)

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
            seats = list(Seat.objects.select_for_update().filter(id__in=seat_ids).order_by('id'))

            if len(seats) != len(seat_ids):
                return Response({"error": "One or more seats not found", "transaction_id": str(transaction_id)}, status=404)

            reservations = list(Reservation.objects.select_for_update().filter(transaction_id=transaction_id))

            if not reservations:
                return Response({"error": "Reservations not found", "transaction_id": str(transaction_id)}, status=404)

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

            Reservation.objects.filter(
                transaction_id=transaction_id,
                seat_id__in=[s.id for s in seats],
                status=Reservation.Status.RESERVED,
            ).update(status=Reservation.Status.BOOKED)

            Seat.objects.filter(id__in=[s.id for s in seats]).update(
                status=Seat.Status.BOOKED,
                held_until=None,
                held_by_transaction_id=transaction_id,
            )

            return Response({"message": "Seats confirmed", "transaction_id": str(transaction_id)}, status=200)
    except DatabaseError as e:
        logger.exception("DatabaseError in confirm_seat: %s", e)
        return Response({"error": "Internal server error occurred.", "code": "DATABASE_ERROR"}, status=500)
    except Exception as e:
        logger.exception("Unexpected error in confirm_seat: %s", e)
        return Response({"error": "Internal server error occurred.", "code": "INTERNAL_ERROR"}, status=500)

@api_view(["POST"])
def release_seat(request):
    # 1. Staff key check (Full admin override)
    is_staff = _is_staff_request(request)

    # 2. Internal service key check (Coordinator Saga rollback)
    auth_err, is_internal = _verify_internal_service_key(request, allow_client=True)
    if auth_err:
        return auth_err

    # 3. User JWT verification (Must be authenticated if not staff/internal)
    auth_user_id = None
    if not (is_staff or is_internal):
        auth_user_id = _get_authenticated_user_id(request)
        if not auth_user_id and not getattr(settings, "IS_TESTING", False):
            return Response(
                {"error": "Authentication required. Please sign in to release a hold.", "code": "AUTH_REQUIRED"},
                status=401,
            )


    raw_tid = request.data.get("transaction_id") if isinstance(request.data, dict) else None
    if not raw_tid:
        return Response({"error": "transaction_id is required", "code": "TRANSACTION_ID_REQUIRED"}, status=400)

    try:
        transaction_id = uuid.UUID(str(raw_tid))
    except (ValueError, TypeError):
        return Response({"error": "transaction_id must be a valid UUID", "code": "INVALID_UUID"}, status=400)

    try:
        candidate_res = list(Reservation.objects.filter(transaction_id=transaction_id))
        if not candidate_res:
            return Response(
                {"message": "No active reservation found to release.", "released_count": 0, "transaction_id": str(transaction_id)},
                status=200,
            )

        candidate_seat_ids = sorted(list({r.seat_id for r in candidate_res if r.seat_id}))

        with transaction.atomic():
            # Step 1: Lock Seats first in ascending ID order
            locked_seats = []
            if candidate_seat_ids:
                locked_seats = list(Seat.objects.select_for_update().filter(id__in=candidate_seat_ids).order_by("id"))

            # Step 2: Lock Reservations second
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

                # Fail fast 2: Regular user attempting to release confirmed/booked ticket
                if not (is_staff or is_internal) and r.status == Reservation.Status.BOOKED:
                    return Response(
                        {
                            "error": "This reservation is already booked and confirmed. Users cannot cancel booked tickets. Please contact staff.",
                            "code": "CANNOT_RELEASE_BOOKED",
                            "transaction_id": str(transaction_id),
                        },
                        status=403,
                    )

                # Collect active reservations to release
                if is_staff or is_internal:
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

            reclaimed_seat_count = 0
            if seat_ids:
                seats_to_update = [
                    s.id for s in locked_seats
                    if s.id in seat_ids and s.held_by_transaction_id == transaction_id
                ]
                if seats_to_update:
                    reclaimed_seat_count = Seat.objects.filter(
                        id__in=seats_to_update
                    ).update(
                        status=Seat.Status.AVAILABLE,
                        held_until=None,
                        held_by_transaction_id=None,
                        user_id=None,
                        user_email=None,
                    )

            return Response(
                {
                    "message": f"Successfully released {reclaimed_seat_count} seat(s).",
                    "released_count": reclaimed_seat_count,
                    "transaction_id": str(transaction_id),
                },
                status=200,
            )

    except DatabaseError as e:
        logger.exception("DatabaseError in release_seat: %s", e)
        return Response({"error": "Internal server error occurred.", "code": "DATABASE_ERROR"}, status=500)
    except Exception as e:
        logger.exception("Unexpected error in release_seat: %s", e)
        return Response({"error": "Internal server error occurred.", "code": "INTERNAL_ERROR"}, status=500)

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
    is_debug = getattr(settings, "DEBUG", False)
    allow_unauth = getattr(settings, "ALLOW_UNAUTHENTICATED_RESET", False)
    if not is_debug or not allow_unauth:
        if not _is_staff_request(request):
            return Response(
                {"error": "Resetting venue seats is restricted. Staff key required."},
                status=403,
            )

    event_id = request.data.get("event_id") if isinstance(request.data, dict) else None
    with transaction.atomic():
        seat_qs = Seat.objects.all()
        res_qs = Reservation.objects.all()
        if event_id:
            try:
                event_id_int = int(event_id)
                seat_qs = seat_qs.filter(event_id=event_id_int)
                res_qs = res_qs.filter(seat__event_id=event_id_int)
            except (ValueError, TypeError):
                return Response({"error": "event_id must be an integer"}, status=400)

        before_counts = {
            "seats": seat_qs.count(),
            "reservations": res_qs.count(),
            "booked_reservations": res_qs.filter(status=Reservation.Status.BOOKED).count(),
        }

        updated_seats = seat_qs.update(
            status=Seat.Status.AVAILABLE,
            held_until=None,
            held_by_transaction_id=None,
            user_id=None,
            user_email=None,
        )

        deleted_res, _ = res_qs.delete()
        rows_affected = updated_seats + deleted_res

        who = "staff" if _is_staff_request(request) else "unauthenticated"
        logger.info(
            "AUDIT: reset_seats executed by who=%s, event_id=%s, rows_affected=%s, before_counts=%s",
            who,
            event_id,
            rows_affected,
            before_counts,
        )

    msg = f"Seats for event {event_id} reset to AVAILABLE." if event_id else "All seats reset to AVAILABLE."
    return Response({"message": msg})



@api_view(["POST"])
def sweep_expired_holds(request):
    """
    Sweeps expired and abandoned seat holds and marks them as AVAILABLE.
    Pure background management / maintenance endpoint.
    Requires internal service key or staff key.
    """
    auth_err, is_internal = _verify_internal_service_key(request)
    if auth_err and not _is_staff_request(request):
        return Response(
            {"error": "Forbidden: Requires internal service key or staff key."},
            status=403,
        )
    reclaimed_count, _ = reclaim_expired_holds()
    return Response({
        "message": f"Swept {reclaimed_count} expired seat hold(s).",
        "reclaimed_count": reclaimed_count,
    }, status=200)

