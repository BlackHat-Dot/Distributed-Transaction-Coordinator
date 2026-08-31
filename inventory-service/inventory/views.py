import os
import uuid
from datetime import timedelta
from django.conf import settings
from django.db import transaction, IntegrityError
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

import redis

from .models import Event, Seat, Reservation
from .serializers import EventSerializer, SeatSerializer, ReservationSerializer


MAX_ACTOR_HOLDS = int(os.getenv("MAX_ACTOR_HOLDS", "10"))

# Atomic Redis Lua script for cumulative per-actor hold cap enforcement
# KEYS[1]: dtc:actor_holds:<actor_id> (Sorted Set: member = seat_id, score = expiry_timestamp)
# ARGV[1]: current_timestamp (float unix epoch)
# ARGV[2]: seat_id (str)
# ARGV[3]: expiry_timestamp (float unix epoch)
# ARGV[4]: max_cap (int)
RESERVE_ACTOR_HOLD_LUA = """
-- 1. Evict expired holds for this actor
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])

-- 2. Check if seat already held by this actor (idempotent hold refresh)
local is_already_held = redis.call('ZSCORE', KEYS[1], ARGV[2])
if is_already_held then
    redis.call('ZADD', KEYS[1], ARGV[3], ARGV[2])
    redis.call('EXPIRE', KEYS[1], 1800)
    return {1, redis.call('ZCARD', KEYS[1])}
end

-- 3. Check active count against cap
local current_count = redis.call('ZCARD', KEYS[1])
if current_count >= tonumber(ARGV[4]) then
    return {0, current_count} -- REJECTED: cap exceeded
end

-- 4. Atomically record new hold
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[2])
redis.call('EXPIRE', KEYS[1], 1800)

return {1, current_count + 1} -- APPROVED
"""

RELEASE_ACTOR_HOLD_LUA = """
redis.call('ZREM', KEYS[1], ARGV[1])
return redis.call('ZCARD', KEYS[1])
"""


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
    events = Event.objects.all().prefetch_related("seats")
    serializer = EventSerializer(events, many=True)
    return Response(serializer.data)


@api_view(["GET"])
def seat_list(request):
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

    seats = Seat.objects.all().select_related("event").order_by("id")
    serializer = SeatSerializer(seats, many=True)
    return Response(serializer.data)


def _validate_uuid(val):
    try:
        return uuid.UUID(str(val))
    except (ValueError, AttributeError):
        return None


def _validate_reserve_input(data):
    if not data:
        return None, None, Response({"message": "no input values"}, status=400)

    seat_id = data.get("seat_id") or data.get("product_id")
    if seat_id is None:
        return None, None, Response({"error": "seat_id is required"}, status=400)

    try:
        seat_id = int(seat_id)
    except (ValueError, TypeError):
        return None, None, Response({"error": "seat_id must be an integer"}, status=400)

    raw_tid = data.get("transaction_id")
    if not raw_tid:
        return None, None, Response({"error": "transaction_id is required"}, status=400)

    tid = _validate_uuid(raw_tid)
    if not tid:
        return None, None, Response({"error": "transaction_id must be a valid UUID"}, status=400)

    return seat_id, tid, None


def _seat_response(seat, reservation, transaction_id, status_code=200):
    user_id = (reservation.user_id if reservation else None) or getattr(seat, "user_id", None) or getattr(seat, "held_by_user_id", None)
    user_email = (reservation.user_email if reservation else None) or getattr(seat, "user_email", None) or getattr(seat, "held_by_user_email", None)
    held_by_user_id = (getattr(reservation, "held_by_user_id", None) if reservation else None) or getattr(seat, "held_by_user_id", None) or user_id
    held_by_user_email = (getattr(reservation, "held_by_user_email", None) if reservation else None) or getattr(seat, "held_by_user_email", None) or user_email
    return Response(
        {
            "message": "Seat reserved",
            "transaction_id": str(transaction_id),
            "status": reservation.status if reservation else Reservation.Status.RESERVED,
            "seat_id": seat.id,
            "event_id": seat.event_id,
            "row": seat.row,
            "seat_number": seat.seat_number,
            "price": str(seat.price),
            "held_until": seat.held_until,
            "held_by_user_id": held_by_user_id,
            "held_by_user_email": held_by_user_email,
            "user_id": user_id,
            "user_email": user_email,
            "actor_id": user_id,
        },
        status=status_code,
    )


@api_view(["POST"])
def reserve_seat(request):
    seat_id, transaction_id, error_response = _validate_reserve_input(request.data)
    if error_response:
        return error_response

    user_id = (
        request.data.get("user_id")
        or request.data.get("held_by_user_id")
        or request.data.get("actor_id")
        or request.headers.get("X-User-ID")
        or request.headers.get("X-Actor-ID")
        or "usr_anonymous"
    )
    user_email = (
        request.data.get("user_email")
        or request.data.get("held_by_user_email")
        or request.data.get("email")
        or request.headers.get("X-User-Email")
        or "anonymous@dtc.io"
    )
    held_by_user_id = request.data.get("held_by_user_id") or user_id
    held_by_user_email = request.data.get("held_by_user_email") or user_email

    client = get_redis_client()
    redis_key = f"seat:{seat_id}"
    acquired_fast_path = False

    # Cumulative Soft-Hold Cap Enforcement (Atomic Redis Lua Script)
    if client and user_id:
        now_ts = timezone.now().timestamp()
        exp_ts = now_ts + 600
        try:
            cap_res = client.eval(
                RESERVE_ACTOR_HOLD_LUA,
                1,
                f"dtc:user_holds:{user_id}",
                now_ts,
                str(seat_id),
                exp_ts,
                MAX_ACTOR_HOLDS,
            )
            # cap_res is [allowed_flag, current_count]
            if cap_res and cap_res[0] == 0:
                return Response(
                    {
                        "error": f"Cumulative hold cap exceeded: You have reached the maximum limit of {MAX_ACTOR_HOLDS} active soft-holds.",
                        "code": "HOLD_CAP_EXCEEDED",
                        "user_id": user_id,
                        "user_email": user_email,
                        "held_by_user_id": held_by_user_id,
                        "held_by_user_email": held_by_user_email,
                        "actor_id": user_id,
                        "current_holds": cap_res[1],
                        "max_allowed": MAX_ACTOR_HOLDS,
                        "transaction_id": str(transaction_id),
                    },
                    status=409,
                )
        except Exception:
            pass

    if client:
        try:
            acquired_fast_path = bool(
                client.set(redis_key, str(transaction_id), nx=True, ex=600)
            )
        except Exception:
            acquired_fast_path = False

    try:
        with transaction.atomic():
            try:
                seat = Seat.objects.select_for_update().get(id=seat_id)
            except Seat.DoesNotExist:
                if client and acquired_fast_path:
                    try:
                        client.delete(redis_key)
                    except Exception:
                        pass
                return Response(
                    {"error": "Seat not found", "transaction_id": str(transaction_id)},
                    status=404,
                )

            now = timezone.now()

            if seat.held_by_transaction_id and str(seat.held_by_transaction_id) == str(transaction_id):
                reservation = Reservation.objects.filter(transaction_id=transaction_id).first()
                return _seat_response(seat, reservation, transaction_id, status_code=200)

            is_available = (seat.status == Seat.Status.AVAILABLE) or (
                seat.status == Seat.Status.HELD and seat.held_until and seat.held_until < now
            )

            if not is_available:
                conflict_reason = (
                    "Seat is already booked"
                    if seat.status == Seat.Status.BOOKED
                    else "Seat is currently held by another user"
                )
                return Response(
                    {
                        "error": conflict_reason,
                        "seat_id": seat.id,
                        "transaction_id": str(transaction_id),
                        "status": seat.status,
                    },
                    status=409,
                )

            seat.status = Seat.Status.HELD
            seat.held_until = now + timedelta(minutes=10)
            seat.held_by_transaction_id = transaction_id
            seat.held_by_user_id = held_by_user_id
            seat.held_by_user_email = held_by_user_email
            seat.user_id = user_id
            seat.user_email = user_email
            seat.save()

            reservation, _ = Reservation.objects.update_or_create(
                transaction_id=transaction_id,
                defaults={
                    "seat": seat,
                    "status": Reservation.Status.RESERVED,
                    "held_by_user_id": held_by_user_id,
                    "held_by_user_email": held_by_user_email,
                    "user_id": user_id,
                    "user_email": user_email,
                },
            )

            if client and not acquired_fast_path:
                try:
                    client.set(redis_key, str(transaction_id), ex=600)
                except Exception:
                    pass

            return _seat_response(seat, reservation, transaction_id, status_code=200)

    except IntegrityError:
        existing = Reservation.objects.filter(transaction_id=transaction_id).first()
        if existing and existing.seat:
            return _seat_response(existing.seat, existing, transaction_id, status_code=200)
        return Response({"error": "Concurrent reservation conflict"}, status=409)


@api_view(["POST"])
def confirm_seat(request):
    raw_tid = request.data.get("transaction_id")
    if not raw_tid:
        return Response({"error": "transaction_id is required"}, status=400)

    tid = _validate_uuid(raw_tid)
    if not tid:
        return Response({"error": "transaction_id must be a valid UUID"}, status=400)

    client = get_redis_client()

    with transaction.atomic():
        reservation = (
            Reservation.objects.select_for_update()
            .filter(transaction_id=tid)
            .first()
        )
        if not reservation or not reservation.seat_id:
            return Response(
                {"error": "Reservation not found", "transaction_id": str(tid)},
                status=404,
            )

        seat = Seat.objects.select_for_update().get(id=reservation.seat_id)
        now = timezone.now()

        if seat.status == Seat.Status.BOOKED and str(seat.held_by_transaction_id) == str(tid):
            return Response(
                {
                    "message": "Seat already confirmed",
                    "seat_id": seat.id,
                    "status": "BOOKED",
                    "transaction_id": str(tid),
                },
                status=200,
            )

        if (
            seat.status != Seat.Status.HELD
            or str(seat.held_by_transaction_id) != str(tid)
            or (seat.held_until and seat.held_until < now)
        ):
            return Response(
                {
                    "error": "Seat hold has expired before confirmation",
                    "seat_id": seat.id,
                    "transaction_id": str(tid),
                    "status": "HOLD_EXPIRED",
                },
                status=409,
            )

        # Release from user's Redis hold set
        user_key_id = seat.held_by_user_id or seat.user_id or getattr(seat, "held_by_actor_id", None)
        if user_key_id and client:
            try:
                client.eval(RELEASE_ACTOR_HOLD_LUA, 1, f"dtc:user_holds:{user_key_id}", str(seat.id))
                client.eval(RELEASE_ACTOR_HOLD_LUA, 1, f"dtc:actor_holds:{user_key_id}", str(seat.id))
            except Exception:
                pass

        user_id = reservation.user_id or getattr(seat, "user_id", None) or getattr(seat, "held_by_user_id", None)
        user_email = reservation.user_email or getattr(seat, "user_email", None) or getattr(seat, "held_by_user_email", None)
        held_by_user_id = getattr(reservation, "held_by_user_id", None) or getattr(seat, "held_by_user_id", None) or user_id
        held_by_user_email = getattr(reservation, "held_by_user_email", None) or getattr(seat, "held_by_user_email", None) or user_email

        seat.status = Seat.Status.BOOKED
        seat.held_until = None
        seat.held_by_user_id = held_by_user_id
        seat.held_by_user_email = held_by_user_email
        seat.user_id = user_id
        seat.user_email = user_email
        seat.save()

        reservation.status = Reservation.Status.BOOKED
        reservation.held_by_user_id = held_by_user_id
        reservation.held_by_user_email = held_by_user_email
        reservation.save()

        if client:
            try:
                client.delete(f"seat:{seat.id}")
            except Exception:
                pass

        return Response(
            {
                "message": "Seat booked successfully",
                "seat_id": seat.id,
                "status": "BOOKED",
                "transaction_id": str(tid),
                "held_by_user_id": held_by_user_id,
                "held_by_user_email": held_by_user_email,
                "user_id": user_id,
                "user_email": user_email,
                "actor_id": user_id,
            },
            status=200,
        )


@api_view(["POST"])
def release_seat(request):
    raw_tid = request.data.get("transaction_id")
    if not raw_tid:
        return Response({"error": "transaction_id is required"}, status=400)

    if raw_tid == "fail-release":
        return Response({"error": "Release failed intentionally"}, status=500)

    tid = _validate_uuid(raw_tid)
    if not tid:
        return Response({"error": "transaction_id must be a valid UUID"}, status=400)

    client = get_redis_client()

    with transaction.atomic():
        reservation = (
            Reservation.objects.select_for_update()
            .filter(transaction_id=tid)
            .first()
        )
        if not reservation:
            return Response(
                {"error": "Reservation not found", "transaction_id": str(tid)},
                status=404,
            )

        if reservation.status == Reservation.Status.RELEASED:
            return Response(
                {
                    "message": "Reservation already released",
                    "transaction_id": str(tid),
                    "held_by_user_id": getattr(reservation, "held_by_user_id", None) or reservation.user_id,
                    "held_by_user_email": getattr(reservation, "held_by_user_email", None) or reservation.user_email,
                    "user_id": reservation.user_id,
                    "user_email": reservation.user_email,
                    "actor_id": reservation.user_id,
                },
                status=200,
            )

        if reservation.seat_id:
            try:
                seat = Seat.objects.select_for_update().get(id=reservation.seat_id)
                if str(seat.held_by_transaction_id) == str(tid):
                    # Evict from user's active Redis hold set
                    user_key_id = seat.held_by_user_id or seat.user_id or getattr(seat, "held_by_actor_id", None)
                    if user_key_id and client:
                        try:
                            client.eval(RELEASE_ACTOR_HOLD_LUA, 1, f"dtc:user_holds:{user_key_id}", str(seat.id))
                            client.eval(RELEASE_ACTOR_HOLD_LUA, 1, f"dtc:actor_holds:{user_key_id}", str(seat.id))
                        except Exception:
                            pass

                    seat.status = Seat.Status.AVAILABLE
                    seat.held_until = None
                    seat.held_by_transaction_id = None
                    seat.held_by_user_id = None
                    seat.held_by_user_email = None
                    seat.user_id = None
                    seat.user_email = None
                    seat.save()

                    if client:
                        try:
                            client.delete(f"seat:{seat.id}")
                        except Exception:
                            pass
            except Seat.DoesNotExist:
                pass

        reservation.status = Reservation.Status.RELEASED
        reservation.save()

        return Response(
            {
                "message": "Seat released",
                "transaction_id": str(tid),
                "status": Reservation.Status.RELEASED,
                "held_by_user_id": getattr(reservation, "held_by_user_id", None) or reservation.user_id,
                "held_by_user_email": getattr(reservation, "held_by_user_email", None) or reservation.user_email,
                "user_id": reservation.user_id,
                "user_email": reservation.user_email,
                "actor_id": reservation.user_id,
            },
            status=200,
        )


@api_view(["GET"])
def reservation_check(request):
    raw_tid = request.query_params.get("transaction_id")
    if not raw_tid:
        return Response({"error": "transaction_id is required"}, status=400)

    tid = _validate_uuid(raw_tid)
    if not tid:
        return Response({"error": "transaction_id must be a valid UUID"}, status=400)

    reservation = Reservation.objects.filter(transaction_id=tid).first()
    if not reservation:
        return Response(
            {
                "transaction_id": str(tid),
                "status": Reservation.Status.NOT_RESERVED,
            }
        )

    return Response(
        {
            "transaction_id": str(tid),
            "status": reservation.status,
            "seat_id": reservation.seat_id,
            "held_by_user_id": getattr(reservation, "held_by_user_id", None) or reservation.user_id,
            "held_by_user_email": getattr(reservation, "held_by_user_email", None) or reservation.user_email,
            "user_id": reservation.user_id,
            "user_email": reservation.user_email,
            "actor_id": reservation.user_id,
        }
    )


@api_view(["POST"])
def sweep_expired_holds(request):
    now = timezone.now()
    expired_seats = list(
        Seat.objects.filter(status=Seat.Status.HELD, held_until__lt=now).values_list(
            "id", flat=True
        )
    )

    reclaimed_count = 0
    client = get_redis_client()

    for s_id in expired_seats:
        with transaction.atomic():
            seat = (
                Seat.objects.select_for_update()
                .filter(id=s_id, status=Seat.Status.HELD, held_until__lt=now)
                .first()
            )
            if not seat:
                continue

            tid = seat.held_by_transaction_id
            user_key_id = seat.held_by_user_id or seat.user_id or getattr(seat, "held_by_actor_id", None)

            if user_key_id and client:
                try:
                    client.eval(RELEASE_ACTOR_HOLD_LUA, 1, f"dtc:user_holds:{user_key_id}", str(seat.id))
                    client.eval(RELEASE_ACTOR_HOLD_LUA, 1, f"dtc:actor_holds:{user_key_id}", str(seat.id))
                except Exception:
                    pass

            seat.status = Seat.Status.AVAILABLE
            seat.held_until = None
            seat.held_by_transaction_id = None
            seat.held_by_user_id = None
            seat.held_by_user_email = None
            seat.user_id = None
            seat.user_email = None
            seat.save()

            if tid:
                Reservation.objects.filter(
                    transaction_id=tid, status=Reservation.Status.RESERVED
                ).update(status=Reservation.Status.RELEASED)

            if client:
                try:
                    client.delete(f"seat:{seat.id}")
                except Exception:
                    pass

            reclaimed_count += 1

    return Response(
        {
            "message": "Expired holds swept successfully",
            "reclaimed_count": reclaimed_count,
        }
    )


@api_view(["POST"])
def reset_seats(request):
    with transaction.atomic():
        if Seat.objects.count() == 0:
            event = Event.objects.first()
            if not event:
                event = Event.objects.create(
                    name="Coldplay: Music of the Spheres World Tour",
                    venue="Grand Arena Stadium",
                    starts_at=timezone.now() + timedelta(days=14),
                )
            for r, price in [("A", 750.00), ("B", 500.00), ("C", 350.00)]:
                for num in range(1, 11):
                    Seat.objects.create(
                        event=event,
                        row=r,
                        seat_number=str(num),
                        price=price,
                        status=Seat.Status.AVAILABLE,
                    )
        else:
            Seat.objects.all().update(
                status=Seat.Status.AVAILABLE,
                held_until=None,
                held_by_transaction_id=None,
            )
        Reservation.objects.all().delete()

    client = get_redis_client()
    if client:
        try:
            keys = client.keys("seat:*")
            if keys:
                client.delete(*keys)
        except Exception:
            pass

    return Response(
        {
            "message": "All venue seats have been reset to AVAILABLE",
            "total_seats": Seat.objects.count(),
            "available_seats": Seat.objects.filter(status=Seat.Status.AVAILABLE).count(),
        },
        status=200,
    )