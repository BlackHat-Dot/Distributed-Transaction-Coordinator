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
    seats = Seat.objects.all().select_related("event")
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

    # Support seat_id (primary) and product_id as seat_id fallback
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
        },
        status=status_code,
    )


@api_view(["POST"])
def reserve_seat(request):
    """
    Two-Phase Hold Phase:
    Atomically transitions a Seat from AVAILABLE to HELD with a 10-minute hold expiry.

    Concurrency & Authority Discipline:
    - Fast-Path Speed Layer: Attempts `SET seat:{seat_id} {tx_id} NX EX 600` in Redis to
      shed concurrent hold storms in-memory.
    - Single Source of Truth: PostgreSQL row locking (`Seat.objects.select_for_update()`) inside
      `transaction.atomic()` is the sole authoritative authority for seat state.
    - If Redis and PostgreSQL ever diverge (e.g. cold cache, key eviction, or crash), PostgreSQL
      strictly governs availability and self-heals Redis.
    """
    seat_id, transaction_id, error_response = _validate_reserve_input(request.data)
    if error_response:
        return error_response

    client = get_redis_client()
    redis_key = f"seat:{seat_id}"
    acquired_fast_path = False

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

            # 1. Idempotency Check: Same transaction already holds or booked this seat
            if seat.held_by_transaction_id and str(seat.held_by_transaction_id) == str(transaction_id):
                reservation = Reservation.objects.filter(transaction_id=transaction_id).first()
                return _seat_response(seat, reservation, transaction_id, status_code=200)

            # 2. Availability Check: Seat is AVAILABLE or has an expired HELD status
            is_available = (seat.status == Seat.Status.AVAILABLE) or (
                seat.status == Seat.Status.HELD and seat.held_until and seat.held_until < now
            )

            if not is_available:
                # Conflict: Seat is already booked or actively held by another transaction
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

            # 3. Transition to HELD
            seat.status = Seat.Status.HELD
            seat.held_until = now + timedelta(minutes=10)
            seat.held_by_transaction_id = transaction_id
            seat.save()

            reservation, _ = Reservation.objects.update_or_create(
                transaction_id=transaction_id,
                defaults={"seat": seat, "status": Reservation.Status.RESERVED},
            )

            # Ensure Redis speed layer is populated if fast-path had a cache-miss
            if client and not acquired_fast_path:
                try:
                    client.set(redis_key, str(transaction_id), ex=600)
                except Exception:
                    pass

            return _seat_response(seat, reservation, transaction_id, status_code=200)

    except IntegrityError:
        # Concurrent duplicate insert on unique constraint
        existing = Reservation.objects.filter(transaction_id=transaction_id).first()
        if existing and existing.seat:
            return _seat_response(existing.seat, existing, transaction_id, status_code=200)
        return Response({"error": "Concurrent reservation conflict"}, status=409)


@api_view(["POST"])
def confirm_seat(request):
    """
    Two-Phase Confirm Phase:
    On payment success, transitions a Seat from HELD to BOOKED.

    Stale Hold Protection:
    If `held_until` has passed before confirmation arrives (e.g. payment was slower
    than the 10-minute hold window), confirmation is REJECTED with HTTP 409 and status
    'HOLD_EXPIRED'. The coordinator catches this and triggers compensation.
    """
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

        # 1. Idempotent return if already booked
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

        # 2. Guard against expired holds or stolen holds
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

        # 3. Transition HELD -> BOOKED
        seat.status = Seat.Status.BOOKED
        seat.held_until = None
        seat.save()

        reservation.status = Reservation.Status.BOOKED
        reservation.save()

        # Delete Redis hold key now that seat is permanently booked
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
            },
            status=200,
        )


@api_view(["POST"])
def release_seat(request):
    """
    Two-Phase Release / Compensation Phase:
    Releases a held seat back to AVAILABLE on payment decline, timeout, or compensation.
    """
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
                },
                status=200,
            )

        if reservation.seat_id:
            try:
                seat = Seat.objects.select_for_update().get(id=reservation.seat_id)
                if str(seat.held_by_transaction_id) == str(tid):
                    seat.status = Seat.Status.AVAILABLE
                    seat.held_until = None
                    seat.held_by_transaction_id = None
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
            },
            status=200,
        )


@api_view(["GET"])
def reservation_check(request):
    """
    Ground-truth query endpoint used by the coordinator crash recovery sweep.
    """
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
        }
    )


@api_view(["POST"])
def sweep_expired_holds(request):
    """
    Expired-Hold Sweeper:
    Finds HELD seats past `held_until` and atomically restores them to AVAILABLE,
    purging the corresponding Redis fast-path keys to maintain lock consistency.
    """
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
            seat.status = Seat.Status.AVAILABLE
            seat.held_until = None
            seat.held_by_transaction_id = None
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
    """
    Demo / Development Helper:
    Resets all seats in the venue catalog back to AVAILABLE, cleans up expired holds,
    and clears all Redis hold keys.
    """
    with transaction.atomic():
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