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
    # Option C: Piggyback Sweep
    # Every time the seat map is loaded, immediately release any expired or abandoned holds
    # in PostgreSQL so the DB always reflects truth for the requesting user.
    reclaim_expired_holds()
    # End Piggyback Sweep


    # Will be deleted once project reaches real theater owners. 
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

    # N+1 query problem solved by select_related — all seats + events in 1 SQL query.
    seats = Seat.objects.all().select_related("event").order_by("id")

    # Convert returned QuerySet containing Python Seat model instances to JSON format
    # By definition: Objects -> JSON
    serializer = SeatSerializer(seats, many=True)
    return Response(serializer.data) # JSON string is sent over the internet


INTERNAL_SERVICE_KEY = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY")


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
        if key != INTERNAL_SERVICE_KEY:
            return Response(
                {"error": "Forbidden: invalid internal service key."},
                status=403,
            ), False
        return None, True

    if allow_client:
        return None, False

    if getattr(settings, "IS_TESTING", False) or settings.DEBUG:
        return None, True

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
        return Response({"error": "seat_ids must be a list of integers"}, status=400)

    seats = Seat.objects.filter(id__in=target_ids).order_by("id")

    # Nothing yet started (Data query hasn't run yet, no loop has run), to_representation() has not been called, No JSON has been created).
    # but because many=True DRF wraps SeatSerializer inside a ListSerializer,
    # it save the variable pointer: self.instance = seats,
    # it sits idle, waiting for instructions.
    serializer = SeatSerializer(seats, many=True) # Think this line as Gun.

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
    return Response(serializer.data) # .data ; the actual trigger.


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
    if not raw_tid:
        return None, None, Response({"error": "transaction_id is required"}, status=400)

    tid = _validate_uuid(raw_tid)
    if not tid:
        return None, None, Response({"error": "transaction_id must be a valid UUID"}, status=400)

    return seat_ids, tid, None


@api_view(["POST"])
def reserve_seat(request):
    auth_err, has_internal_key = _verify_internal_service_key(request, allow_client=True) # Service key checker
    if auth_err:
        return auth_err

    seat_ids, transaction_id, error_response = _validate_reserve_input(request.data)

    if error_response:
        return error_response

    # 'None' is used to allows admin/Staff to buy tickets without mentioning their ids
    user_id = request.data.get("user_id") or None
    user_email = request.data.get("user_email") or None

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
                # .get_or_Create() gives `object, True` if object is created if not then `object, False`

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
    auth_err, is_internal = _verify_internal_service_key(request, allow_client=True)
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
                # seat id is stored
                seat_ids = [r.seat_id for r in active_res if r.seat_id] 

                # Update all those active reservation (status with RESERVED) status as RELEASED
                Reservation.objects.filter(id__in=[r.id for r in active_res]).update( 
                    status=Reservation.Status.RELEASED
                )

                """
                FIX: Seat Release Cross-Transaction Cancellation Vulnerability

                1. Previous Flaw:
                   The code previously used:
                       filter(Q(held_by_transaction_id=transaction_id) | Q(status=Seat.Status.BOOKED))
                   The naked `Q(status=BOOKED)` clause lacked transaction ownership validation.

                2. Race Condition / Exploit:
                   - User A holds Seat 5 (tx_A), but the 10-minute timer expires without purchase.
                   - User B comes in, reserves, and pays for Seat 5 (tx_B). The seat is now BOOKED.
                   - User A's browser sends a delayed/late "Cancel Hold" for tx_A.
                   - The old query checked:
                         held_by_transaction_id == tx_A (False) OR status == BOOKED (True) -> MATCH!
                   - Result: User A's late cancel wiped out User B's paid, confirmed ticket back to AVAILABLE!

                3. The Solution:
                   Both `reserve_seat` (HELD) and `confirm_seat` (BOOKED) save the transaction's
                   UUID into `seat.held_by_transaction_id`.

                   By strictly filtering with:
                       filter(id__in=seat_ids, held_by_transaction_id=transaction_id)
                   we guarantee a transaction can NEVER release a seat unless it is the verified
                   owner, completely eliminating the hold-expiry race condition.
                """

                if seat_ids:
                    # update all seats as AVAILABLE
                    Seat.objects.filter(
                        id__in=seat_ids,
                        held_by_transaction_id=transaction_id  # Must belong to THIS transaction!
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


STAFF_API_KEY = os.getenv("STAFF_API_KEY")

@api_view(["POST"])
def reset_seats(request):
    if not settings.DEBUG:
        if request.headers.get("X-Staff-Key") != STAFF_API_KEY:
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

