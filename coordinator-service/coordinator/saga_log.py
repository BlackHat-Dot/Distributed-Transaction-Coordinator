import json
import logging
import threading
import time
import redis
from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import Max
from django.utils import timezone

from .models import Transaction, SagaEvent

logger = logging.getLogger(__name__)

_pub_client = None
_pub_last_check = 0.0
_pub_lock = threading.Lock()


def _get_pub_client():
    """
    Thread-safe, self-healing Redis publisher client.
    Guarded by double-checked locking and a 5.0s circuit-breaker cooldown.
    """
    global _pub_client, _pub_last_check

    if _pub_client is not None:
        return _pub_client

    with _pub_lock:
        if _pub_client is not None:
            return _pub_client

        now = time.monotonic()
        if now - _pub_last_check < 5.0:
            return None

        _pub_last_check = now
        broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
        testing_mode = bool(getattr(settings, "IS_TESTING", False))
        timeout = getattr(settings, "REDIS_SOCKET_TIMEOUT", 0.05 if testing_mode else 1.0)

        client = None
        try:
            client = redis.Redis.from_url(
                broker_url,
                decode_responses=True,
                socket_connect_timeout=timeout,
                socket_timeout=timeout,
            )
            client.ping()
            _pub_client = client
            return _pub_client
        except Exception as e:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            _pub_client = None
            logger.warning("Failed to initialize Redis publisher client: %s", e)
            return None


TERMINAL_STATUSES = Transaction.TERMINAL_STATUSES

ALLOWED_TRANSITIONS = {
    Transaction.Status.STARTED: {
        Transaction.Status.INVENTORY_RESERVED,
        Transaction.Status.HOLDING,
        Transaction.Status.INVENTORY_RESERVE_FAILED,
        Transaction.Status.INVENTORY_TIMEOUT,
        Transaction.Status.INVENTORY_CONNECTION_ERROR,
        Transaction.Status.TIMEOUT,
        Transaction.Status.COMPENSATING,
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    Transaction.Status.HOLDING: {
        Transaction.Status.INVENTORY_RESERVED,
        Transaction.Status.TIMEOUT,
        Transaction.Status.COMPENSATING,
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    Transaction.Status.INVENTORY_RESERVED: {
        Transaction.Status.PAYMENT_PROCESSING,
        Transaction.Status.HOLDING,
        Transaction.Status.INVENTORY_RESERVE_FAILED,
        Transaction.Status.TIMEOUT,
        Transaction.Status.COMPENSATING,
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    Transaction.Status.PAYMENT_PROCESSING: {
        Transaction.Status.COMPLETED,
        Transaction.Status.PAYMENT_FAILED,
        Transaction.Status.PAYMENT_TIMEOUT,
        Transaction.Status.PAYMENT_CONNECTION_ERROR,
        Transaction.Status.COMPENSATING,
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    Transaction.Status.PAYMENT_TIMEOUT: {
        Transaction.Status.COMPENSATING,
        Transaction.Status.COMPLETED,
        Transaction.Status.RECOVERY_IN_PROGRESS,
        Transaction.Status.TIMEOUT,
    },
    Transaction.Status.PAYMENT_CONNECTION_ERROR: {
        Transaction.Status.COMPENSATING,
        Transaction.Status.COMPLETED,
        Transaction.Status.RECOVERY_IN_PROGRESS,
        Transaction.Status.TIMEOUT,
    },
    Transaction.Status.INVENTORY_TIMEOUT: {
        Transaction.Status.INVENTORY_RESERVE_FAILED,
        Transaction.Status.RECOVERY_IN_PROGRESS,
        Transaction.Status.TIMEOUT,
    },
    Transaction.Status.INVENTORY_CONNECTION_ERROR: {
        Transaction.Status.INVENTORY_RESERVE_FAILED,
        Transaction.Status.RECOVERY_IN_PROGRESS,
        Transaction.Status.TIMEOUT,
    },
    Transaction.Status.COMPENSATING: {
        Transaction.Status.COMPLETED,
        Transaction.Status.COMPENSATED,
        Transaction.Status.COMPENSATION_FAILED,
        Transaction.Status.RECOVERY_IN_PROGRESS,
        Transaction.Status.COMPENSATED_ON_RETRY,
        Transaction.Status.RETRY_COMPENSATION_FAILED,
        Transaction.Status.PAYMENT_TIMEOUT,
        Transaction.Status.PAYMENT_CONNECTION_ERROR,
        Transaction.Status.INVENTORY_TIMEOUT,
        Transaction.Status.INVENTORY_CONNECTION_ERROR,
    },
    Transaction.Status.COMPENSATION_FAILED: {
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    Transaction.Status.RETRY_COMPENSATION_FAILED: {
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    Transaction.Status.RECOVERY_IN_PROGRESS: {
        Transaction.Status.COMPENSATING,
        Transaction.Status.COMPLETED,
        Transaction.Status.COMPENSATED,
        Transaction.Status.COMPENSATED_ON_RETRY,
        Transaction.Status.COMPENSATION_FAILED,
        Transaction.Status.RETRY_COMPENSATION_FAILED,
        Transaction.Status.TIMEOUT,
        # Reversion targets when sweeper or retry encounters transport error
        Transaction.Status.STARTED,
        Transaction.Status.HOLDING,
        Transaction.Status.INVENTORY_RESERVED,
        Transaction.Status.PAYMENT_PROCESSING,
        Transaction.Status.PAYMENT_TIMEOUT,
        Transaction.Status.PAYMENT_CONNECTION_ERROR,
        Transaction.Status.INVENTORY_TIMEOUT,
        Transaction.Status.INVENTORY_CONNECTION_ERROR,
    },
}


def _publish_transaction_update(transaction_id, to_status, from_status=None):
    """Publish real-time transaction state update to Redis pub/sub channel for SSE streaming."""
    global _pub_client
    try:
        client = _get_pub_client()
        if not client:
            return
        payload = json.dumps({
            "transaction_id": str(transaction_id),
            "status": to_status,
            "from_status": from_status,
            "is_terminal": to_status in Transaction.TERMINAL_STATUSES,
            "needs_recovery": to_status in Transaction.RECOVERABLE_STATUSES,
        })
        client.publish(f"dtc:tx_stream:{transaction_id}", payload)
    except Exception as exc:
        logger.warning("Failed to publish SSE event for tx %s: %s", transaction_id, exc)
        with _pub_lock:
            if _pub_client is not None:
                try:
                    _pub_client.close()
                except Exception:
                    pass
                _pub_client = None


SENSITIVE_KEYS = {
    "user_email", "email", "card_number", "credit_card", "pan", "cvv", "cvc",
    "password", "token", "otp", "secret", "authorization",
    "api_key", "apikey", "access_token", "refresh_token", "private_key",
    "secret_key", "staff_key", "x-staff-key",
}


def _mask_pii(data):
    """
    Recursively masks sensitive fields (email, credentials, tokens) in audit payloads.
    """
    if not data:
        return data
    if isinstance(data, dict):
        masked = {}
        for k, v in data.items():
            clean_k = str(k).strip().lower()
            if clean_k in SENSITIVE_KEYS:
                if isinstance(v, str) and "@" in v:
                    parts = v.split("@", 1)
                    user_part = parts[0]
                    masked_user = (user_part[0] + "***" + user_part[-1]) if len(user_part) > 2 else "***"
                    masked[k] = f"{masked_user}@{parts[1]}"
                elif isinstance(v, str) and len(v) > 4:
                    masked[k] = f"****{v[-4:]}"
                else:
                    masked[k] = "******"
            elif isinstance(v, (dict, list)):
                masked[k] = _mask_pii(v)
            else:
                masked[k] = v
        return masked
    elif isinstance(data, list):
        return [_mask_pii(item) for item in data]
    return data


# verified
def _set_status_with_event(
    transaction_id,
    to_status,
    expected_status=None,
    actor=SagaEvent.ACTOR_REQUEST,
    http_status_code=None,
    http_payload=None,
):
    """
    Execute compare-and-swap (CAS) status update within an exclusive database transaction lock.
    Records an append-only SagaEvent audit log entry and registers an on_commit SSE broadcast.
    """
    if not transaction_id:
        return False

    if to_status not in Transaction.Status.values:
        logger.warning(
            "Transition aborted for tx %s: %r is invalid; caller must use a valid Transaction.Status enum constant",
            transaction_id,
            to_status,
        )
        return False

    with db_transaction.atomic():
        qs = Transaction.objects.select_for_update().filter(
            transaction_id=transaction_id
        )
        if expected_status is not None:
            if isinstance(expected_status, (list, tuple, set)):
                qs = qs.filter(status__in=[s for s in expected_status if s is not None])
            else:
                qs = qs.filter(status=expected_status)

        rows = list(qs.values("id", "status"))
        if not rows:
            return False

        txn_pk = rows[0]["id"]
        from_status = rows[0]["status"]

        if to_status == from_status:
            return True

        # Validate CAS transition against ALLOWED_TRANSITIONS
        if expected_status is not None and to_status != from_status:
            allowed = ALLOWED_TRANSITIONS.get(from_status, set())
            if to_status not in allowed:
                logger.warning(
                    "Illegal CAS state transition rejected for tx %s: %s -> %s (allowed: %s)",
                    transaction_id,
                    from_status,
                    to_status,
                    allowed,
                )
                return False

        Transaction.objects.filter(pk=txn_pk).update(
            status=to_status,
            updated_at=timezone.now(),
        )

        last_seq = (
            SagaEvent.objects.filter(transaction_id=txn_pk)
            .aggregate(m=Max("seq"))["m"]
            or 0
        )
        next_seq = last_seq + 1

        SagaEvent.objects.create(
            transaction_id=txn_pk,
            seq=next_seq,
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            http_status_code=http_status_code,
            http_payload=_mask_pii(http_payload),
        )

        # Defer Redis SSE publish until the outermost DB transaction successfully commits
        db_transaction.on_commit(
            lambda tid=transaction_id, ts=to_status, fs=from_status: _publish_transaction_update(tid, ts, fs)
        )

    return True