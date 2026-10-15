import logging
import uuid
import zoneinfo
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.response import Response

from .auth import (
    _get_authenticated_user,
    _get_authenticated_user_id,
    _is_staff_request,
    _verify_internal_service_key,
)
from .models import Event, Reservation, Seat
from .serializers import EventSerializer, InternalSeatSerializer, PublicSeatSerializer
from .services import (
    _validate_reserve_input,
    get_hold_duration,
    get_redis_client,
    reclaim_expired_holds,
)
from .throttling import (
    CheckReservationRateThrottle,
    ReleaseAnonRateThrottle,
    ReleaseRateThrottle,
    ReserveAnonRateThrottle,
    ReserveRateThrottle,
    SeatListRateThrottle,
)

logger = logging.getLogger(__name__)


@api_view(["GET"])
def health_check(request):
    return Response({"status": "healthy", "service": "inventory"})


@api_view(["GET"])
def ready_check(request):
    """Return 200 when the database and Redis are reachable, otherwise 503."""
    db_ok = False
    try:
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_ok = True
    except Exception as exc:
        logger.warning("Database readiness check failed: %s", exc)

    redis_ok = False
    try:
        client = get_redis_client()
        if client is not None and client.ping():
            redis_ok = True
        else:
            logger.warning("Redis readiness check failed: client is None or ping failed")
    except Exception as exc:
        logger.warning("Redis readiness check failed: %s", exc)

    is_ready = db_ok and redis_ok
    return Response(
        {
            "status": "ready" if is_ready else "not_ready",
            "database": "connected" if db_ok else "disconnected",
            "redis": "connected" if redis_ok else "disconnected",
            "service": "inventory",
        },
        status=200 if is_ready else 503,
    )


@api_view(["GET"])
def event_list(request):
    # prefetch_related runs a separate seats query instead of a JOIN
    events = Event.objects.all().prefetch_related("seats")
    return Response(EventSerializer(events, many=True).data)


def _seed_demo_seats():
    """Create the demo event and 30 seats (rows A-C, 10 each)."""
    event = Event.objects.first() or Event.objects.create(
        name="Coldplay: Music of the Spheres World Tour",
        venue="Grand Arena Stadium",
        starts_at=datetime(2026, 11, 15, 19, 30, tzinfo=zoneinfo.ZoneInfo("Asia/Kolkata")),
    )
    Seat.objects.bulk_create([
        Seat(event=event, row=row, seat_number=str(num), price=price, status=Seat.Status.AVAILABLE)
        for row, price in [("A", Decimal("750.00")), ("B", Decimal("500.00")), ("C", Decimal("350.00"))]
        for num in range(1, 11)
    ])


@api_view(["GET"])
@throttle_classes([SeatListRateThrottle])
def seat_list(request):
    # Expired holds are shown as AVAILABLE by the serializer; background sweeps reclaim them
    if getattr(settings, "DEBUG", False) and Seat.objects.count() == 0:
        _seed_demo_seats()

    seats = Seat.objects.all()
    event_id = request.query_params.get("event_id")
    if event_id:
        try:
            seats = seats.filter(event_id=int(event_id))
        except (ValueError, TypeError):
            return Response({"error": "event_id must be an integer"}, status=400)

    serializer = PublicSeatSerializer(seats.order_by("id"), many=True, context={"request": request})
    return Response(serializer.data)


@api_view(["POST"])
def seat_pricing(request):
    """Return seat rows for seat_ids. Coordinator only (service key required)."""
    auth_err, _ = _verify_internal_service_key(request)
    if auth_err:
        return auth_err

    if not isinstance(request.data, dict):
        return Response({"error": "Request body must be a JSON object", "code": "INVALID_BODY"}, status=400)

    seat_ids = request.data.get("seat_ids")
    if not seat_ids or not isinstance(seat_ids, list):
        return Response({"error": "seat_ids is required and must be a list"}, status=400)

    if any(type(s) is not int for s in seat_ids):
        return Response({"error": "seat_ids must be a list of integers"}, status=400)

    seats = Seat.objects.filter(id__in=seat_ids).order_by("id")
    return Response(InternalSeatSerializer(seats, many=True).data)


@api_view(["POST"])
@throttle_classes([ReserveRateThrottle, ReserveAnonRateThrottle])
def reserve_seat(request):
    auth_err, has_internal_key = _verify_internal_service_key(request, allow_client=True)
    if auth_err:
        return auth_err

    seat_ids, supplied_tid, error_response = _validate_reserve_input(request.data)
    if error_response:
        return error_response

    # Coordinator sends its saga UUID; browser callers always get a fresh one
    if has_internal_key:
        if not supplied_tid:
            return Response({"error": "transaction_id is required"}, status=400)
        transaction_id = supplied_tid
    else:
        transaction_id = uuid.uuid4()

    # Identity: internal key trusts the body; otherwise a verified JWT;
    # unverified headers only in testing or when explicitly allowed
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

    is_testing = getattr(settings, "IS_TESTING", False)
    is_dev = getattr(settings, "DEBUG", False)
    if not (is_dev or is_testing or has_internal_key) and not user_id:
        return Response(
            {
                "error": "Authentication required. Your session has expired or you are not signed in. Please sign in to hold seats.",
                "code": "AUTHENTICATION_REQUIRED",
            },
            status=401,
        )

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

    tx_str = str(transaction_id)

    try:
        with transaction.atomic():
            # Serialize hold checks per user (PostgreSQL advisory lock)
            if user_id and connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"user_hold:{user_id}"])

            seats = list(Seat.objects.select_for_update().filter(id__in=seat_ids).order_by("id"))
            if len(seats) != len(seat_ids):
                return Response({"error": "One or more seats not found", "transaction_id": tx_str}, status=404)

            now = timezone.now()

            if user_id and max_user_holds is not None:
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
                            "transaction_id": tx_str,
                        },
                        status=429,
                    )

            # Seats this transaction already booked: all of them means idempotent success
            booked_by_this_tx = [
                s for s in seats
                if s.status == Seat.Status.BOOKED and str(s.held_by_transaction_id) == tx_str
            ]
            if booked_by_this_tx:
                if len(booked_by_this_tx) == len(seats):
                    total_amount = sum(s.price for s in seats)
                    return Response(
                        {
                            "message": f"{len(seats)} Seats reserved",
                            "transaction_id": tx_str,
                            "seat_ids": [s.id for s in seats],
                            "held_until": None,
                            "total_amount": str(total_amount),
                        },
                        status=200,
                    )
                return Response(
                    {
                        "error": "Conflict: Partial reservation mismatch. Some requested seats are already booked for this transaction.",
                        "code": "PARTIAL_SEATS_BOOKED_CONFLICT",
                        "transaction_id": tx_str,
                    },
                    status=409,
                )

            for seat in seats:
                if seat.status == Seat.Status.BOOKED:
                    return Response(
                        {
                            "error": f"Seat {seat.row}{seat.seat_number} is already booked",
                            "code": "SEAT_UNAVAILABLE",
                            "transaction_id": tx_str,
                        },
                        status=409,
                    )

                # Already held by this transaction (idempotent retry)
                if seat.status == Seat.Status.HELD and seat.held_by_transaction_id and str(seat.held_by_transaction_id) == tx_str:
                    continue

                # Held by the same user (moving a temporary hold to checkout)
                if seat.is_held and not seat.is_expired(now) and user_id and str(seat.user_id) == str(user_id):
                    continue

                # Free, or held but expired
                if seat.is_available or (seat.is_held and seat.is_expired(now)):
                    continue

                return Response(
                    {
                        "error": f"Seat {seat.row}{seat.seat_number} is already booked or held",
                        "code": "SEAT_UNAVAILABLE",
                        "transaction_id": tx_str,
                    },
                    status=409,
                )

            held_until = now + get_hold_duration()
            for seat in seats:
                # Mark any unfinished reservation of the previous holder as released
                if seat.held_by_transaction_id and str(seat.held_by_transaction_id) != tx_str:
                    Reservation.objects.filter(
                        seat=seat,
                        transaction_id=seat.held_by_transaction_id,
                        status=Reservation.Status.RESERVED,
                    ).update(status=Reservation.Status.RELEASED)

                seat.status = Seat.Status.HELD
                seat.held_until = held_until
                seat.held_by_transaction_id = transaction_id
                seat.user_id = user_id
                seat.user_email = user_email
                seat.save(update_fields=["status", "held_until", "held_by_transaction_id", "user_id", "user_email"])

                reservation, created = Reservation.objects.get_or_create(
                    seat=seat,
                    transaction_id=transaction_id,
                    defaults={"user_id": user_id, "user_email": user_email, "status": Reservation.Status.RESERVED},
                )
                if not created:
                    reservation.status = Reservation.Status.RESERVED
                    reservation.user_id = user_id
                    reservation.user_email = user_email
                    reservation.save(update_fields=["status", "user_id", "user_email"])

            total_amount = sum(seat.price for seat in seats)
            return Response(
                {
                    "message": f"{len(seats)} Seats reserved",
                    "transaction_id": tx_str,
                    "seat_ids": seat_ids,
                    "held_until": held_until,
                    "total_amount": str(total_amount),
                },
                status=200,
            )

    except IntegrityError as exc:
        logger.warning("[transaction_id=%s] IntegrityError in reserve_seat: %s", transaction_id, exc)
        return Response(
            {
                "error": "Conflict: Seat reservation conflict or concurrent modification.",
                "code": "SEAT_CONFLICT",
                "transaction_id": tx_str,
            },
            status=409,
        )
    except DatabaseError as exc:
        logger.exception("[transaction_id=%s] DatabaseError in reserve_seat: %s", transaction_id, exc)
        return Response({"error": "Internal server error occurred.", "code": "DATABASE_ERROR"}, status=500)
    except Exception as exc:
        logger.exception("[transaction_id=%s] Unexpected error in reserve_seat: %s", transaction_id, exc)
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

    tx_str = str(transaction_id)

    try:
        with transaction.atomic():
            seats = list(Seat.objects.select_for_update().filter(id__in=seat_ids).order_by("id"))
            if len(seats) != len(seat_ids):
                return Response({"error": "One or more seats not found", "transaction_id": tx_str}, status=404)

            reservations = list(Reservation.objects.select_for_update().filter(transaction_id=transaction_id))
            if not reservations:
                return Response({"error": "Reservations not found", "transaction_id": tx_str}, status=404)

            now = timezone.now()

            already_booked = []
            for seat in seats:
                if seat.status == Seat.Status.BOOKED and str(seat.held_by_transaction_id) == tx_str:
                    already_booked.append(seat)
                    continue

                # Seat is held by another transaction
                if str(seat.held_by_transaction_id) != tx_str:
                    released = [r for r in reservations if r.status == Reservation.Status.RELEASED]
                    released_reason = (released[0].released_reason or "UNKNOWN_LEGACY") if released else "EXPIRED_HOLD"
                    return Response(
                        {
                            "error": f"Seat {seat.row}{seat.seat_number} hold does not belong to transaction {transaction_id}",
                            "code": "HOLD_EXPIRED",
                            "status": "HOLD_EXPIRED",
                            "transaction_id": tx_str,
                            "released_reason": released_reason,
                        },
                        status=409,
                    )

                # Held by this transaction but past its hold time
                if seat.held_until and seat.held_until < now:
                    return Response(
                        {
                            "error": f"Seat {seat.row}{seat.seat_number} hold has expired before confirmation",
                            "code": "HOLD_EXPIRED",
                            "status": "HOLD_EXPIRED",
                            "transaction_id": tx_str,
                        },
                        status=409,
                    )

            to_book = [s for s in seats if s not in already_booked]

            if already_booked:
                booked_count = Reservation.objects.filter(
                    transaction_id=transaction_id,
                    seat_id__in=[s.id for s in already_booked],
                    status=Reservation.Status.BOOKED,
                ).count()
                if booked_count != len(already_booked):
                    transaction.set_rollback(True)
                    return Response(
                        {
                            "error": "Reservation state mismatch during confirmation.",
                            "code": "RESERVATION_STATE_MISMATCH",
                            "status": "RESERVATION_STATE_MISMATCH",
                            "transaction_id": tx_str,
                        },
                        status=409,
                    )

            if to_book:
                to_book_ids = [s.id for s in to_book]
                updated_count = Reservation.objects.filter(
                    transaction_id=transaction_id,
                    seat_id__in=to_book_ids,
                    status=Reservation.Status.RESERVED,
                ).update(status=Reservation.Status.BOOKED)
                if updated_count != len(to_book):
                    transaction.set_rollback(True)
                    return Response(
                        {
                            "error": "Reservation state mismatch during confirmation.",
                            "code": "RESERVATION_STATE_MISMATCH",
                            "status": "RESERVATION_STATE_MISMATCH",
                            "transaction_id": tx_str,
                        },
                        status=409,
                    )

                Seat.objects.filter(id__in=to_book_ids).update(
                    status=Seat.Status.BOOKED,
                    held_until=None,
                    held_by_transaction_id=transaction_id,
                )

            return Response({"message": "Seats confirmed", "transaction_id": tx_str}, status=200)

    except DatabaseError as exc:
        logger.exception("[transaction_id=%s] DatabaseError in confirm_seat: %s", transaction_id, exc)
        return Response({"error": "Internal server error occurred.", "code": "DATABASE_ERROR"}, status=500)
    except Exception as exc:
        logger.exception("[transaction_id=%s] Unexpected error in confirm_seat: %s", transaction_id, exc)
        return Response({"error": "Internal server error occurred.", "code": "INTERNAL_ERROR"}, status=500)


@api_view(["POST"])
@throttle_classes([ReleaseRateThrottle, ReleaseAnonRateThrottle])
def release_seat(request):
    is_staff = _is_staff_request(request)
    auth_err, is_internal = _verify_internal_service_key(request, allow_client=True)
    if auth_err:
        return auth_err
    privileged = is_staff or is_internal

    # Regular users must be signed in (unless testing)
    auth_user_id = None
    if not privileged:
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

    no_active_response = {
        "message": "No active reservation found to release.",
        "released_count": 0,
        "transaction_id": str(transaction_id),
    }

    try:
        candidates = list(Reservation.objects.filter(transaction_id=transaction_id))
        if not candidates:
            return Response(no_active_response, status=200)

        candidate_seat_ids = sorted({r.seat_id for r in candidates if r.seat_id})

        with transaction.atomic():
            # Lock seats first (ascending ID), then reservations
            locked_seats = list(
                Seat.objects.select_for_update().filter(id__in=candidate_seat_ids).order_by("id")
            )
            reservations = list(Reservation.objects.select_for_update().filter(transaction_id=transaction_id))
            if not reservations:
                return Response(no_active_response, status=200)

            releasable_statuses = (
                (Reservation.Status.RESERVED, Reservation.Status.BOOKED)
                if privileged
                else (Reservation.Status.RESERVED,)
            )

            active_res = []
            for r in reservations:
                if not privileged and (not r.user_id or not auth_user_id or str(r.user_id) != str(auth_user_id)):
                    return Response(
                        {"error": "Forbidden: You do not own this reservation hold.", "code": "HOLD_OWNERSHIP_MISMATCH"},
                        status=403,
                    )

                if not privileged and r.status == Reservation.Status.BOOKED:
                    return Response(
                        {
                            "error": "This reservation is already booked and confirmed. Users cannot cancel booked tickets. Please contact staff.",
                            "code": "CANNOT_RELEASE_BOOKED",
                            "transaction_id": str(transaction_id),
                        },
                        status=403,
                    )

                if r.status in releasable_statuses:
                    active_res.append(r)

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
            release_reason = "COORDINATOR_COMPENSATED" if privileged else "USER_CANCELLED"
            Reservation.objects.filter(id__in=[r.id for r in active_res]).update(
                status=Reservation.Status.RELEASED,
                released_reason=release_reason,
            )

            # Free only the seats this transaction still holds
            seats_to_free = [
                s.id for s in locked_seats
                if s.id in seat_ids and str(s.held_by_transaction_id) == str(transaction_id)
            ]
            released_count = 0
            if seats_to_free:
                released_count = Seat.objects.filter(id__in=seats_to_free).update(
                    status=Seat.Status.AVAILABLE,
                    held_until=None,
                    held_by_transaction_id=None,
                    user_id=None,
                    user_email=None,
                )

            return Response(
                {
                    "message": f"Successfully released {released_count} seat(s).",
                    "released_count": released_count,
                    "transaction_id": str(transaction_id),
                },
                status=200,
            )

    except DatabaseError as exc:
        logger.exception("[transaction_id=%s] DatabaseError in release_seat: %s", transaction_id, exc)
        return Response({"error": "Internal server error occurred.", "code": "DATABASE_ERROR"}, status=500)
    except Exception as exc:
        logger.exception("[transaction_id=%s] Unexpected error in release_seat: %s", transaction_id, exc)
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

    tid = str(transaction_id)
    reservations = list(Reservation.objects.select_related("seat", "seat__event").filter(transaction_id=transaction_id))
    if not reservations:
        return Response({"status": Reservation.Status.NOT_RESERVED, "transaction_id": tid}, status=200)

    if any(r.status == Reservation.Status.BOOKED for r in reservations):
        if any(r.seat and r.seat.held_by_transaction_id and str(r.seat.held_by_transaction_id) != tid for r in reservations):
            return Response(
                {
                    "status": Reservation.Status.RELEASED,
                    "transaction_id": tid,
                    "released_reason": "SEAT_TAKEN_BY_ANOTHER_TRANSACTION",
                },
                status=200,
            )

        booked = [r for r in reservations if r.status == Reservation.Status.BOOKED]
        first_seat = booked[0].seat
        event = first_seat.event if first_seat else None
        event_data = None
        if event:
            event_data = {
                "name": event.name,
                "venue": event.venue,
                "starts_at": event.starts_at.isoformat() if event.starts_at else None,
            }
        seats_data = [
            {
                "seat_id": r.seat.id,
                "seat_label": f"{r.seat.row}{r.seat.seat_number}",
                "amount": str(r.seat.price),
            }
            for r in booked if r.seat
        ]
        return Response(
            {"status": Reservation.Status.BOOKED, "transaction_id": tid, "event": event_data, "seats": seats_data},
            status=200,
        )

    released = next((r for r in reservations if r.status == Reservation.Status.RELEASED), None)
    if released is not None:
        return Response(
            {
                "status": Reservation.Status.RELEASED,
                "transaction_id": tid,
                "released_reason": released.released_reason or "UNKNOWN_LEGACY",
            },
            status=200,
        )

    now = timezone.now()
    if any(
        r.seat and (
            (r.seat.held_until and r.seat.held_until < now)
            or (r.seat.held_by_transaction_id and str(r.seat.held_by_transaction_id) != tid)
        )
        for r in reservations
    ):
        return Response(
            {"status": Reservation.Status.RELEASED, "transaction_id": tid, "released_reason": "EXPIRED_HOLD"},
            status=200,
        )

    return Response({"status": Reservation.Status.RESERVED, "transaction_id": tid}, status=200)


@api_view(["POST"])
def reset_seats(request):
    is_staff = _is_staff_request(request)
    unauthenticated_reset_allowed = (
        getattr(settings, "DEBUG", False) and getattr(settings, "ALLOW_UNAUTHENTICATED_RESET", False)
    )
    if not unauthenticated_reset_allowed and not is_staff:
        return Response({"error": "Resetting venue seats is restricted. Staff key required."}, status=403)

    event_id = request.data.get("event_id") if isinstance(request.data, dict) else None

    with transaction.atomic():
        seat_qs = Seat.objects.all()
        res_qs = Reservation.objects.all()
        if event_id:
            try:
                event_id_int = int(event_id)
            except (ValueError, TypeError):
                return Response({"error": "event_id must be an integer"}, status=400)
            seat_qs = seat_qs.filter(event_id=event_id_int)
            res_qs = res_qs.filter(seat__event_id=event_id_int)

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

        # Keep booked history: mark RELEASED rather than delete
        updated_booked = res_qs.filter(status=Reservation.Status.BOOKED).update(
            status=Reservation.Status.RELEASED,
            released_reason="ADMIN_RESET",
        )
        deleted_res, _ = res_qs.exclude(status=Reservation.Status.RELEASED).delete()
        rows_affected = updated_seats + updated_booked + deleted_res

        logger.info(
            "AUDIT: reset_seats executed by who=%s, event_id=%s, rows_affected=%s, before_counts=%s",
            "staff" if is_staff else "unauthenticated",
            event_id,
            rows_affected,
            before_counts,
        )

    if event_id:
        message = f"Seats for event {event_id} reset to AVAILABLE."
    else:
        message = "All seats reset to AVAILABLE."
    return Response({"message": message})


@api_view(["POST"])
def sweep_expired_holds(request):
    """Background maintenance: reclaim expired holds. Internal key or staff key required."""
    auth_err, _ = _verify_internal_service_key(request)
    if auth_err and not _is_staff_request(request):
        return Response({"error": "Forbidden: Requires internal service key or staff key."}, status=403)

    reclaimed_count, _ = reclaim_expired_holds()
    return Response(
        {"message": f"Swept {reclaimed_count} expired seat hold(s).", "reclaimed_count": reclaimed_count},
        status=200,
    )