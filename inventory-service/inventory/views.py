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
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.response import Response

import jwt
from .models import Event, Seat, Reservation
from .serializers import EventSerializer, PublicSeatSerializer, InternalSeatSerializer
from .throttling import (
    UserActionRateThrottle,
    AnonIpRateThrottle,
    SeatListRateThrottle,
    ReserveRateThrottle,
    ReserveAnonRateThrottle,
    ReleaseRateThrottle,
    ReleaseAnonRateThrottle,
    CheckReservationRateThrottle,
)
from .auth import (
    _is_staff_request,
    _get_authenticated_user,
    _get_authenticated_user_id,
    _verify_internal_service_key,
)
from .services import (
    get_redis_client,
    get_hold_duration,
    reclaim_expired_holds,
    _validate_uuid,
    _validate_reserve_input,
    _redis_client_instance,
    _redis_last_failure,
    _REDIS_NEGATIVE_CACHE_TTL,
)

logger = logging.getLogger(__name__)


@api_view(["GET"])
def health_check(request):
    return Response({"status": "healthy", "service": "inventory"})


@api_view(["GET"])
def ready_check(request):
    """
    Readiness probe verifying database and Redis connectivity.
    Returns 200 if both are reachable, 503 if any core dependency is down.
    """
    db_ok = False
    redis_ok = False
    try:
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_ok = True
    except Exception as e:
        logger.warning("Database readiness check failed: %s", e)

    try:
        r = get_redis_client()
        if r is not None and r.ping():
            redis_ok = True
        else:
            logger.warning("Redis readiness check failed: client is None or ping failed")
    except Exception as e:
        logger.warning("Redis readiness check failed: %s", e)

    is_ready = db_ok and redis_ok
    status_code = 200 if is_ready else 503
    return Response(
        {
            "status": "ready" if is_ready else "not_ready",
            "database": "connected" if db_ok else "disconnected",
            "redis": "connected" if redis_ok else "disconnected",
            "service": "inventory",
        },
        status=status_code,
    )

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


@api_view(["GET"])
@throttle_classes([SeatListRateThrottle])
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

    event_id = request.query_params.get("event_id")
    seats = Seat.objects.all()
    if event_id:
        try:
            seats = seats.filter(event_id=int(event_id))
        except (ValueError, TypeError):
            return Response({"error": "event_id must be an integer"}, status=400)
    seats = seats.order_by("id")
    serializer = PublicSeatSerializer(seats, many=True, context={"request": request})
    return Response(serializer.data)


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

    if not isinstance(request.data, dict):
        return Response({"error": "Request body must be a JSON object", "code": "INVALID_BODY"}, status=400)

    seat_ids = request.data.get("seat_ids")

    if not seat_ids or not isinstance(seat_ids, list):
        return Response({"error": "seat_ids is required and must be a list"}, status=400)

    for s in seat_ids:
        if type(s) is not int or isinstance(s, bool):
            return Response({"error": "seat_ids must be a list of integers"}, status=400)

    target_ids = list(dict.fromkeys(seat_ids))

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


@api_view(["POST"])
@throttle_classes([ReserveRateThrottle, ReserveAnonRateThrottle])
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

    if has_internal_key:
        if not supplied_tid:
            return Response({"error": "transaction_id is required"}, status=400)
        transaction_id = supplied_tid
    else:
        transaction_id = uuid.uuid4()  # Always freshly generated for browser callers

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

    if user_id is not None:
        user_id = str(user_id)
    if user_email is not None:
        user_email = str(user_email).strip().lower()

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
    max_user_holds = getattr(settings, "MAX_USER_HOLDS", 10)
    
    if max_user_holds is not None and len(seat_ids) > max_user_holds:
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


            if user_id and max_user_holds is not None:
                # Exclude objects which is held by current transaction id.
                active_holds = Seat.objects.filter(
                    user_id=user_id,
                    status=Seat.Status.HELD,
                    held_until__gt=now,
                ).exclude(held_by_transaction_id=transaction_id).count()

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
                if seat.is_held and not seat.is_expired(now) and user_id and str(seat.user_id) == str(user_id):
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

            held_until_dt = now + get_hold_duration()
            
            reservations = []
            for seat in seats:
                # Clean up any unfinalized hold on this seat so it doesn't get orphaned
                if seat.held_by_transaction_id and str(seat.held_by_transaction_id) != str(transaction_id):
                    Reservation.objects.filter(
                        seat=seat,
                        transaction_id=seat.held_by_transaction_id,
                        status=Reservation.Status.RESERVED,
                    ).update(status=Reservation.Status.RELEASED)

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
        logger.warning("[transaction_id=%s] IntegrityError in reserve_seat: %s", transaction_id if "transaction_id" in locals() else None, e)
        return Response(
            {
                "error": "Conflict: Seat reservation conflict or concurrent modification.",
                "code": "SEAT_CONFLICT",
                "transaction_id": str(transaction_id) if "transaction_id" in locals() and transaction_id else None,
            },
            status=409,
        )
    except DatabaseError as e:
        logger.exception("[transaction_id=%s] DatabaseError in reserve_seat: %s", transaction_id if "transaction_id" in locals() else None, e)
        return Response({"error": "Internal server error occurred.", "code": "DATABASE_ERROR"}, status=500)
    except Exception as e:
        logger.exception("[transaction_id=%s] Unexpected error in reserve_seat: %s", transaction_id if "transaction_id" in locals() else None, e)
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

            already_booked_seats = []
            for seat in seats:
                # This condition checks whether this seat object is booked by this transaction id
                if seat.status == Seat.Status.BOOKED and str(seat.held_by_transaction_id) == str(transaction_id):
                    already_booked_seats.append(seat)
                    continue

                # This condition checks whether this seat object is held by this transaction id
                if str(seat.held_by_transaction_id) != str(transaction_id):
                    released_res = [r for r in reservations if r.status == Reservation.Status.RELEASED]
                    rel_reason = (released_res[0].released_reason or "UNKNOWN_LEGACY") if released_res else "EXPIRED_HOLD"
                    return Response(
                        {
                            "error": f"Seat {seat.row}{seat.seat_number} hold does not belong to transaction {transaction_id}",
                            "code": "HOLD_EXPIRED",
                            "status": "HOLD_EXPIRED",
                            "transaction_id": str(transaction_id),
                            "released_reason": rel_reason,
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

            expected_to_book_seats = [s for s in seats if s not in already_booked_seats]

            # For seats already booked by this transaction, verify their reservations are also BOOKED
            if already_booked_seats:
                booked_res_count = Reservation.objects.filter(
                    transaction_id=transaction_id,
                    seat_id__in=[s.id for s in already_booked_seats],
                    status=Reservation.Status.BOOKED,
                ).count()
                if booked_res_count != len(already_booked_seats):
                    transaction.set_rollback(True)
                    return Response(
                        {
                            "error": "Reservation state mismatch during confirmation.",
                            "code": "RESERVATION_STATE_MISMATCH",
                            "status": "RESERVATION_STATE_MISMATCH",
                            "transaction_id": str(transaction_id),
                        },
                        status=409,
                    )

            if expected_to_book_seats:
                updated_count = Reservation.objects.filter(
                    transaction_id=transaction_id,
                    seat_id__in=[s.id for s in expected_to_book_seats],
                    status=Reservation.Status.RESERVED,
                ).update(status=Reservation.Status.BOOKED)

                if updated_count != len(expected_to_book_seats):
                    transaction.set_rollback(True)
                    return Response(
                        {
                            "error": "Reservation state mismatch during confirmation.",
                            "code": "RESERVATION_STATE_MISMATCH",
                            "status": "RESERVATION_STATE_MISMATCH",
                            "transaction_id": str(transaction_id),
                        },
                        status=409,
                    )

                Seat.objects.filter(id__in=[s.id for s in expected_to_book_seats]).update(
                    status=Seat.Status.BOOKED,
                    held_until=None,
                    held_by_transaction_id=transaction_id,
                )

            return Response({"message": "Seats confirmed", "transaction_id": str(transaction_id)}, status=200)
    except DatabaseError as e:
        logger.exception("[transaction_id=%s] DatabaseError in confirm_seat: %s", transaction_id if "transaction_id" in locals() else None, e)
        return Response({"error": "Internal server error occurred.", "code": "DATABASE_ERROR"}, status=500)
    except Exception as e:
        logger.exception("[transaction_id=%s] Unexpected error in confirm_seat: %s", transaction_id if "transaction_id" in locals() else None, e)
        return Response({"error": "Internal server error occurred.", "code": "INTERNAL_ERROR"}, status=500)

@api_view(["POST"])
@throttle_classes([ReleaseRateThrottle, ReleaseAnonRateThrottle])
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
                if not (is_staff or is_internal):
                    if not r.user_id or not auth_user_id or str(r.user_id) != str(auth_user_id):
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

            rel_reason = "COORDINATOR_COMPENSATED" if (is_staff or is_internal) else "USER_CANCELLED"
            Reservation.objects.filter(id__in=[r.id for r in active_res]).update(
                status=Reservation.Status.RELEASED,
                released_reason=rel_reason,
            )

            reclaimed_seat_count = 0
            if seat_ids:
                seats_to_update = [
                    s.id for s in locked_seats
                    if s.id in seat_ids and str(s.held_by_transaction_id) == str(transaction_id)
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
        logger.exception("[transaction_id=%s] DatabaseError in release_seat: %s", transaction_id if "transaction_id" in locals() else None, e)
        return Response({"error": "Internal server error occurred.", "code": "DATABASE_ERROR"}, status=500)
    except Exception as e:
        logger.exception("[transaction_id=%s] Unexpected error in release_seat: %s", transaction_id if "transaction_id" in locals() else None, e)
        return Response({"error": "Internal server error occurred.", "code": "INTERNAL_ERROR"}, status=500)

@api_view(["GET"])
@throttle_classes([CheckReservationRateThrottle])
def check_reservation(request):
    raw_tid = request.query_params.get("transaction_id")
    if not raw_tid:
        return Response({"error": "transaction_id is required"}, status=400)
        
    try:
        transaction_id = uuid.UUID(raw_tid)
    except ValueError:
        return Response({"error": "transaction_id must be a valid UUID"}, status=400)

    reservations = list(Reservation.objects.select_related("seat").filter(transaction_id=transaction_id))

    if not reservations:
        return Response({"status": Reservation.Status.NOT_RESERVED, "transaction_id": str(transaction_id)}, status=200)

    if any(r.status == Reservation.Status.BOOKED for r in reservations):
        return Response({"status": Reservation.Status.BOOKED, "transaction_id": str(transaction_id)}, status=200)
    
    if any(r.status == Reservation.Status.RELEASED for r in reservations):
        rel_r = next(r for r in reservations if r.status == Reservation.Status.RELEASED)
        resp_data = {
            "status": Reservation.Status.RELEASED,
            "transaction_id": str(transaction_id),
            "released_reason": rel_r.released_reason or "UNKNOWN_LEGACY",
        }
        return Response(resp_data, status=200)

    now = timezone.now()
    if any(
        r.seat and (
            (r.seat.held_until and r.seat.held_until < now)
            or (r.seat.held_by_transaction_id and str(r.seat.held_by_transaction_id) != str(transaction_id))
        )
        for r in reservations
    ):
        return Response({"status": Reservation.Status.RELEASED, "transaction_id": str(transaction_id), "released_reason": "EXPIRED_HOLD"}, status=200)

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

        # F5/G6: Do not hard-delete BOOKED reservations; mark them RELEASED with released_reason="ADMIN_RESET"
        updated_booked = res_qs.filter(status=Reservation.Status.BOOKED).update(
            status=Reservation.Status.RELEASED,
            released_reason="ADMIN_RESET",
        )
        deleted_res, _ = res_qs.exclude(status=Reservation.Status.RELEASED).delete()
        rows_affected = updated_seats + updated_booked + deleted_res

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

