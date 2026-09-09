import os
import uuid
from datetime import timedelta
from django.conf import settings
from django.db import transaction, IntegrityError
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

import redis

from .models import Event, Seat, Reservation
from .serializers import EventSerializer, SeatSerializer, ReservationSerializer

MAX_ACTOR_HOLDS = int(os.getenv("MAX_ACTOR_HOLDS", "10"))

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

    seat_ids = data.get("seat_ids")
    if not seat_ids and data.get("seat_id"):
        seat_ids = [data.get("seat_id")]
    if not seat_ids:
        return None, None, Response({"error": "seat_ids is required"}, status=400)

    try:
        seat_ids = [int(s) for s in seat_ids]
    except (ValueError, TypeError):
        return None, None, Response({"error": "seat_ids must be a list of integers"}, status=400)

    raw_tid = data.get("transaction_id")
    if not raw_tid:
        return None, None, Response({"error": "transaction_id is required"}, status=400)

    tid = _validate_uuid(raw_tid)
    if not tid:
        return None, None, Response({"error": "transaction_id must be a valid UUID"}, status=400)

    return seat_ids, tid, None

INTERNAL_SERVICE_KEY = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv(
    "INTERNAL_SERVICE_KEY", "dtc-internal-coordinator-secret-key-2026"
)


def _verify_internal_service_key(request, allow_client=False):
    """
    Ensures internal state-mutating endpoints (confirm, release-booked)
    can only be called by internal services (e.g. coordinator) presenting
    the shared secret.
    If allow_client=True (e.g. reserve_seat or user hold cancel), calls without
    the header are permitted (subject to actor quota and validation), but invalid
    keys are always rejected with 403.
    """
    key = request.headers.get("X-Internal-Service-Key")
    if key is not None:
        if key != INTERNAL_SERVICE_KEY:
            return Response(
                {"error": "Forbidden: invalid internal service key."},
                status=403,
            )
        return None

    if allow_client or getattr(settings, "IS_TESTING", False):
        return None

    if not settings.DEBUG:
        return Response(
            {"error": "Forbidden: missing internal service key."},
            status=403,
        )

    return None


@api_view(["POST"])
def reserve_seat(request):
    auth_err = _verify_internal_service_key(request, allow_client=True)
    if auth_err:
        return auth_err

    seat_ids, transaction_id, error_response = _validate_reserve_input(request.data)
    if error_response:
        return error_response

    user_id = (
        request.data.get("user_id") or request.data.get("held_by_user_id") or request.data.get("actor_id")
        or request.headers.get("X-User-ID") or request.headers.get("X-Actor-ID") or "usr_anonymous"
    )
    user_email = (
        request.data.get("user_email") or request.data.get("held_by_user_email") or request.data.get("email")
        or request.headers.get("X-User-Email") or "anonymous@dtc.io"
    )

    try:
        with transaction.atomic():
            seats = list(Seat.objects.select_for_update().filter(id__in=seat_ids).order_by('id'))
            if len(seats) != len(seat_ids):
                return Response({"error": "One or more seats not found", "transaction_id": str(transaction_id)}, status=404)

            now = timezone.now()

            # Enforce MAX_ACTOR_HOLDS (D-5)
            max_actor_holds = getattr(settings, "MAX_ACTOR_HOLDS", MAX_ACTOR_HOLDS)
            if len(seats) > max_actor_holds:
                return Response(
                    {
                        "error": f"Requested seats count ({len(seats)}) exceeds maximum allowed active holds ({max_actor_holds}).",
                        "code": "MAX_HOLDS_EXCEEDED",
                        "active_holds": 0,
                        "max_holds": max_actor_holds,
                        "transaction_id": str(transaction_id),
                    },
                    status=429,
                )

            actor_filter = Q()
            if user_id and user_id != "usr_anonymous":
                actor_filter |= Q(user_id=user_id)
            if user_email and user_email != "anonymous@dtc.io":
                actor_filter |= Q(user_email=user_email)

            if actor_filter:
                active_holds = Seat.objects.filter(
                    actor_filter,
                    status=Seat.Status.HELD,
                    held_until__gt=now,
                ).exclude(held_by_transaction_id=transaction_id).count()
                if active_holds + len(seats) > max_actor_holds:
                    return Response(
                        {
                            "error": f"Actor exceeds maximum allowed active holds ({max_actor_holds}).",
                            "code": "MAX_HOLDS_EXCEEDED",
                            "active_holds": active_holds,
                            "max_holds": max_actor_holds,
                            "transaction_id": str(transaction_id),
                        },
                        status=429,
                    )
            
            for seat in seats:
                if seat.held_by_transaction_id and str(seat.held_by_transaction_id) == str(transaction_id):
                    continue
                is_available = (seat.status == Seat.Status.AVAILABLE) or (seat.status == Seat.Status.HELD and seat.held_until and seat.held_until < now)
                if not is_available:
                    return Response({"error": f"Seat {seat.row}{seat.seat_number} is already booked or held", "code": "SEAT_UNAVAILABLE", "transaction_id": str(transaction_id)}, status=409)

            held_until_dt = now + timedelta(minutes=10)
            
            reservations = []
            for seat in seats:
                seat.status = Seat.Status.HELD
                seat.held_until = held_until_dt
                seat.held_by_transaction_id = transaction_id
                seat.user_id = user_id
                seat.user_email = user_email
                seat.save()
                
                res, created = Reservation.objects.get_or_create(
                    seat=seat,
                    transaction_id=transaction_id,
                    defaults={'user_id': user_id, 'user_email': user_email, 'status': Reservation.Status.RESERVED}
                )
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
    auth_err = _verify_internal_service_key(request)
    if auth_err:
        return auth_err

    seat_ids, transaction_id, error_response = _validate_reserve_input(request.data)
    if error_response:
        return error_response

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
                if seat.status == Seat.Status.BOOKED and str(seat.held_by_transaction_id) == str(transaction_id):
                    continue

                if not seat.held_by_transaction_id or str(seat.held_by_transaction_id) != str(transaction_id):
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
    key = request.headers.get("X-Internal-Service-Key")
    is_internal = (key == INTERNAL_SERVICE_KEY)
    auth_err = _verify_internal_service_key(request, allow_client=True)
    if auth_err:
        return auth_err

    seat_ids, transaction_id, error_response = _validate_reserve_input(request.data)
    if error_response:
        return error_response

    try:
        with transaction.atomic():
            reservations = list(Reservation.objects.select_for_update().filter(transaction_id=transaction_id))
            if not reservations:
                return Response({"message": "No active reservation to release", "transaction_id": str(transaction_id)}, status=200)

            if is_internal or settings.DEBUG or getattr(settings, "IS_TESTING", False):
                active_res = [r for r in reservations if r.status in (Reservation.Status.RESERVED, Reservation.Status.BOOKED)]
            else:
                # Client callers can only release their own temporary holds (RESERVED), not finalized BOOKED seats
                active_res = [r for r in reservations if r.status == Reservation.Status.RESERVED]

            if active_res:
                seat_ids = [r.seat_id for r in active_res if r.seat_id]
                Reservation.objects.filter(id__in=[r.id for r in active_res]).update(
                    status=Reservation.Status.RELEASED
                )
                if seat_ids:
                    Seat.objects.filter(id__in=seat_ids).filter(
                        Q(held_by_transaction_id=transaction_id) | Q(status=Seat.Status.BOOKED)
                    ).update(
                        status=Seat.Status.AVAILABLE,
                        held_until=None,
                        held_by_transaction_id=None,
                        user_id=None,
                        user_email=None,
                    )

            return Response({"message": "Seats released", "transaction_id": str(transaction_id)}, status=200)
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
        is_staff = bool(
            (request.user and request.user.is_authenticated and request.user.is_staff)
            or (request.headers.get("X-Staff-Key") and request.headers.get("X-Staff-Key") == os.getenv("STAFF_API_KEY", "dtc-internal-staff-secret"))
        )
        if not is_staff:
            return Response(
                {"error": "Resetting venue seats is restricted to staff in production."},
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
        Reservation.objects.all().delete()
    return Response({"message": "All seats reset to AVAILABLE."})


@api_view(["POST"])
def sweep_expired_holds(request):
    """
    Sweeps expired seat holds and marks them as AVAILABLE.
    Pure background management / maintenance endpoint.
    """
    now = timezone.now()
    with transaction.atomic():
        expired_seats = list(
            Seat.objects.select_for_update().filter(
                status=Seat.Status.HELD,
                held_until__lt=now,
            )
        )
        reclaimed_count = len(expired_seats)
        if reclaimed_count > 0:
            expired_seat_ids = [s.id for s in expired_seats]
            Reservation.objects.filter(
                seat_id__in=expired_seat_ids,
                status=Reservation.Status.RESERVED,
            ).update(status=Reservation.Status.RELEASED)

            Seat.objects.filter(id__in=expired_seat_ids).update(
                status=Seat.Status.AVAILABLE,
                held_until=None,
                held_by_transaction_id=None,
                user_id=None,
                user_email=None,
            )

    return Response({
        "message": f"Swept {reclaimed_count} expired seat hold(s).",
        "reclaimed_count": reclaimed_count,
    }, status=200)

