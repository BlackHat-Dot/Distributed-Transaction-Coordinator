import os
import uuid
import time
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
    now = timezone.now()

    # Active Sweeper: Find involuntary hold expirations
    expired_seats = list(Seat.objects.filter(status=Seat.Status.HELD, held_until__lt=now))
    if expired_seats:
        with transaction.atomic():
            for seat in expired_seats:
                seat.status = Seat.Status.AVAILABLE
                seat.held_until = None
                seat.held_by_transaction_id = None
                seat.user_id = None
                seat.user_email = None
                seat.save(update_fields=['status', 'held_until', 'held_by_transaction_id', 'user_id', 'user_email'])

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

@api_view(["POST"])
def reserve_seat(request):
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
            
            resp_data = {
                "message": f"{len(seats)} Seats reserved",
                "transaction_id": str(transaction_id),
                "seat_ids": seat_ids,
                "held_until": held_until_dt,
            }
            return Response(resp_data, status=200)

    except Exception as e:
        return Response({"error": "Database error", "detail": str(e)}, status=500)

@api_view(["POST"])
def confirm_seat(request):
    seat_ids, transaction_id, error_response = _validate_reserve_input(request.data)
    if error_response:
        return error_response

    try:
        with transaction.atomic():
            reservations = list(Reservation.objects.select_for_update().filter(transaction_id=transaction_id))
            if not reservations:
                return Response({"error": "Reservations not found", "transaction_id": str(transaction_id)}, status=404)

            for res in reservations:
                res.status = Reservation.Status.BOOKED
                res.save()
                
                if res.seat:
                    res.seat.status = Seat.Status.BOOKED
                    res.seat.held_until = None
                    res.seat.save()
            
            return Response({"message": "Seats confirmed", "transaction_id": str(transaction_id)}, status=200)
    except Exception as e:
        return Response({"error": "Database error", "detail": str(e)}, status=500)

@api_view(["POST"])
def release_seat(request):
    seat_ids, transaction_id, error_response = _validate_reserve_input(request.data)
    if error_response:
        return error_response

    try:
        with transaction.atomic():
            reservations = list(Reservation.objects.select_for_update().filter(transaction_id=transaction_id))
            if not reservations:
                return Response({"message": "No active reservation to release", "transaction_id": str(transaction_id)}, status=200)

            for res in reservations:
                if res.status in (Reservation.Status.RESERVED, Reservation.Status.BOOKED):
                    res.status = Reservation.Status.RELEASED
                    res.save()
                    if res.seat and (res.seat.held_by_transaction_id == transaction_id or res.seat.status == Seat.Status.BOOKED):
                        res.seat.status = Seat.Status.AVAILABLE
                        res.seat.held_until = None
                        res.seat.held_by_transaction_id = None
                        res.seat.user_id = None
                        res.seat.user_email = None
                        res.seat.save()

            return Response({"message": "Seats released", "transaction_id": str(transaction_id)}, status=200)
    except Exception as e:
        return Response({"error": "Database error", "detail": str(e)}, status=500)

@api_view(["POST"])
def check_reservation(request):
    _, transaction_id, error_response = _validate_reserve_input(request.data)
    if error_response:
        return error_response

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
