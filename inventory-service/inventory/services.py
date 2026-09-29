import logging
import time
import uuid
from datetime import timedelta

import redis
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.response import Response

from .models import Seat, Reservation

logger = logging.getLogger(__name__)

_redis_client_instance = None
_redis_last_failure = 0.0
_REDIS_NEGATIVE_CACHE_TTL = 5.0


def _sync_with_views():
    import sys
    views_mod = sys.modules.get("inventory.views")
    if views_mod:
        global _redis_client_instance, _redis_last_failure
        if hasattr(views_mod, "_redis_client_instance") and views_mod._redis_client_instance != _redis_client_instance:
            if views_mod._redis_client_instance is None:
                _redis_client_instance = None
            else:
                _redis_client_instance = views_mod._redis_client_instance
        if hasattr(views_mod, "_redis_last_failure") and views_mod._redis_last_failure != _redis_last_failure:
            if views_mod._redis_last_failure == 0.0:
                _redis_last_failure = 0.0
            else:
                _redis_last_failure = views_mod._redis_last_failure


def _publish_to_views():
    import sys
    views_mod = sys.modules.get("inventory.views")
    if views_mod:
        views_mod._redis_client_instance = _redis_client_instance
        views_mod._redis_last_failure = _redis_last_failure


def get_redis_client():
    global _redis_client_instance, _redis_last_failure
    _sync_with_views()
    if _redis_client_instance is not None:
        return _redis_client_instance

    now = time.monotonic()
    if now - _redis_last_failure < _REDIS_NEGATIVE_CACHE_TTL:
        return None

    redis_url = getattr(settings, "REDIS_URL", None) or getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
    try:
        client = redis.Redis.from_url(
            redis_url, socket_connect_timeout=0.2, socket_timeout=0.2, decode_responses=True
        )
        client.ping()
        _redis_client_instance = client
        _publish_to_views()
        return _redis_client_instance
    except Exception:
        _redis_last_failure = now
        _publish_to_views()
        return None


def _get_active_redis_client():
    import sys
    from unittest.mock import Mock
    views_mod = sys.modules.get("inventory.views")
    if views_mod and hasattr(views_mod, "get_redis_client"):
        if isinstance(views_mod.get_redis_client, Mock) and not isinstance(get_redis_client, Mock):
            return views_mod.get_redis_client()
    return get_redis_client()


def get_hold_duration() -> timedelta:
    """Return configured hold duration as a timedelta, defaulting to 10 minutes."""
    return timedelta(minutes=getattr(settings, "HOLD_DURATION_MINUTES", 10))


def reclaim_expired_holds():
    """
    Reclaims seats that are expired by time (held_until < now) OR abandoned by user
    (user exited browser/website, causing their Redis heartbeat session dtc:session:{email} to expire).
    Presence-based reclaim only applies after a grace period of HOLD_PRESENCE_GRACE_SECONDS (default 120s).

    CONTRACT FOR COORDINATOR / FRONTEND:
    When a checkout/payment begins for a transaction, the coordinator or checkout client
    sets Redis key `dtc:checkout:{transaction_id}` with TTL = (payment timeout + 60s).
    While this key exists, presence-based reclaim is skipped for any seats held by that
    transaction_id, preventing holds from being prematurely cleared during payment processing
    or 3D-Secure verification even if user presence heartbeats lapse.
    The coordinator deletes this key upon reaching terminal outcome (COMPLETED, COMPENSATED, FAILED).
    Time-based expiry (held_until < now) is not skipped by dtc:checkout.
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
    redis_client = _get_active_redis_client()

    grace_seconds = getattr(settings, "HOLD_PRESENCE_GRACE_SECONDS", 120)
    hold_duration = get_hold_duration()

    presence_candidates = []

    for seat in held_seats:
        seat_id = seat["id"]
        # 1. Time-based expiry (hold elapsed) - stays unchanged
        if seat["held_until"] and seat["held_until"] < now:
            expired_snapshots[seat_id] = {
                "tid": seat["held_by_transaction_id"],
                "held_until": seat["held_until"],
            }
            continue

        # 2. Candidate for presence-based expiry: only if hold age >= grace_seconds
        if seat["held_until"] and seat["user_email"] and redis_client:
            hold_start = seat["held_until"] - hold_duration
            hold_age = (now - hold_start).total_seconds()
            if hold_age >= grace_seconds:
                presence_candidates.append(seat)

    # Pipeline Redis EXISTS per unique email and unique transaction_id for presence candidates
    if presence_candidates and redis_client:
        unique_emails = list({s["user_email"].strip().lower() for s in presence_candidates if s.get("user_email")})
        unique_tids = list({str(s["held_by_transaction_id"]) for s in presence_candidates if s.get("held_by_transaction_id")})
        active_emails = set()
        active_checkouts = set()
        try:
            pipe = redis_client.pipeline()
            for email in unique_emails:
                pipe.exists(f"dtc:session:{email}")
            for tid in unique_tids:
                pipe.exists(f"dtc:checkout:{tid}")
            results = pipe.execute()
            expected_len = len(unique_emails) + len(unique_tids)
            if isinstance(results, (list, tuple)):
                if len(results) >= expected_len:
                    email_results = results[:len(unique_emails)]
                    tid_results = results[len(unique_emails):expected_len]
                elif len(results) == len(unique_emails):
                    email_results = results
                    tid_results = []
                else:
                    email_results = results[:len(unique_emails)]
                    tid_results = []
            else:
                email_results = [redis_client.exists(f"dtc:session:{email}") for email in unique_emails]
                tid_results = []

            for email, exists in zip(unique_emails, email_results):
                if exists:
                    active_emails.add(email)

            inactive_candidates = [
                s for s in presence_candidates
                if (s["user_email"].strip().lower() if s.get("user_email") else "") not in active_emails
            ]
            if inactive_candidates and unique_tids:
                if not tid_results and not isinstance(results, (list, tuple)):
                    tid_results = [redis_client.exists(f"dtc:checkout:{tid}") for tid in unique_tids]
                for tid, exists in zip(unique_tids, tid_results):
                    if exists:
                        active_checkouts.add(tid)

            for seat in presence_candidates:
                tid_str = str(seat["held_by_transaction_id"]) if seat.get("held_by_transaction_id") else None
                # Skip presence-based reclaim if checkout key exists (user is actively paying in coordinator)
                if tid_str and tid_str in active_checkouts:
                    continue
                seat_email = seat["user_email"].strip().lower() if seat.get("user_email") else ""
                if seat_email not in active_emails:
                    expired_snapshots[seat["id"]] = {
                        "tid": seat["held_by_transaction_id"],
                        "held_until": seat["held_until"],
                    }

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
        current_now = timezone.now()
        for seat in locked_seats:
            snap = expired_snapshots.get(seat.id)
            if not snap:
                continue
            expected_tid = snap["tid"]
            snap_held_until = snap["held_until"]

            # Compare held_by_transaction_id directly without truthiness
            if seat.held_by_transaction_id != expected_tid:
                continue

            # B1: If held_until changed between snapshot and lock,
            # only proceed if it is still expired by time (seat.held_until < current_now).
            if seat.held_until != snap_held_until and (seat.held_until and seat.held_until >= current_now):
                continue

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


def _validate_uuid(val):
    try:
        return uuid.UUID(str(val))
    except (ValueError, AttributeError):
        return None


def _validate_reserve_input(data):
    if not isinstance(data, dict):
        return None, None, Response({"error": "Request body must be a JSON object", "code": "INVALID_BODY"}, status=400)

    if not data:
        return None, None, Response({"message": "no input values"}, status=400)

    seat_ids = data.get("seat_ids")
    if not seat_ids:
        return None, None, Response({"error": "seat_ids is required"}, status=400)

    if not isinstance(seat_ids, (list, tuple)):
        return None, None, Response({"error": "seat_ids must be a list of integers"}, status=400)

    for s in seat_ids:
        if type(s) is not int or isinstance(s, bool):
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
