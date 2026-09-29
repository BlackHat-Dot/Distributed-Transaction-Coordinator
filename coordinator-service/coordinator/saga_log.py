import json
import logging
import redis
from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import Max
from django.utils import timezone

from .models import Transaction, SagaEvent

logger = logging.getLogger(__name__)

_pub_client = None


def _get_pub_client():
    global _pub_client
    if _pub_client is None:
        broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
        try:
            _pub_client = redis.Redis.from_url(
                broker_url,
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
        except Exception as e:
            logger.warning("Failed to initialize Redis publisher client: %s", e)
            return None
    return _pub_client


TERMINAL_STATUSES = Transaction.TERMINAL_STATUSES

ALLOWED_TRANSITIONS = {
    Transaction.Status.STARTED: {
        Transaction.Status.INVENTORY_RESERVED,
        Transaction.Status.HOLDING,
        Transaction.Status.INVENTORY_RESERVE_FAILED,
        Transaction.Status.INVENTORY_TIMEOUT,
        Transaction.Status.INVENTORY_CONNECTION_ERROR,
        Transaction.Status.TIMEOUT,
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    Transaction.Status.HOLDING: {
        Transaction.Status.INVENTORY_RESERVED,
        Transaction.Status.TIMEOUT,
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    Transaction.Status.INVENTORY_RESERVED: {
        Transaction.Status.PAYMENT_PROCESSING,
        Transaction.Status.HOLDING,
        Transaction.Status.INVENTORY_RESERVED,
        Transaction.Status.INVENTORY_RESERVE_FAILED,
        Transaction.Status.TIMEOUT,
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
        Transaction.Status.COMPENSATED,
        Transaction.Status.COMPENSATION_FAILED,
        Transaction.Status.RECOVERY_IN_PROGRESS,
        Transaction.Status.COMPENSATED_ON_RETRY,
        Transaction.Status.RETRY_COMPENSATION_FAILED,
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


SENSITIVE_KEYS = {
    "user_email", "email", "card_number", "credit_card", "pan", "cvv", "cvc",
    "password", "token", "otp", "secret", "authorization",
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
            if str(k).lower() in SENSITIVE_KEYS:
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


def _set_status_with_event(
    transaction_id,
    to_status,
    expected_status=None,
    actor=SagaEvent.ACTOR_REQUEST,
    http_status_code=None,
    http_payload=None,
):
    with db_transaction.atomic():
        qs = Transaction.objects.select_for_update().filter(
            transaction_id=transaction_id
        )
        if expected_status:
            if isinstance(expected_status, (list, tuple, set)):
                qs = qs.filter(status__in=expected_status)
            else:
                qs = qs.filter(status=expected_status)

        rows = list(qs.values("id", "status"))
        """
        rows = [
            {
                "id": 42,                        # Primary key (auto-increment integer)
                "status": "PAYMENT_PROCESSING"   # Current status column value
            }
        ]
        """
        if not rows:
            return False

        txn_pk = rows[0]["id"] # 42
        from_status = rows[0]["status"] # "PAYMENT_PROCESSING"

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

    # filter(transaction_id=txn_pk): Filters the SagaEvent table to only include events belonging to this specific transaction.
    # .aggregate(m=Max("seq")): Tells PostgreSQL to run an aggregate SQL query:
    # like, SELECT MAX("seq") AS m FROM "coordinator_sagaevent" WHERE "transaction_id" = 42;
    # SagaEvent.objects.filter(transaction_id=txn_pk).aggregate(m=Max("seq")) --> result_dict = {"m": 3} --> result_dict["m"]
    # if the sagaevent record doesn't exist then None returned , None or 0 --> 0

        last_seq = (
            SagaEvent.objects.filter(transaction_id=txn_pk)
            .aggregate(m=Max("seq"))["m"]
            or 0
        )
        next_seq = last_seq + 1 # new/prev sequence +1 

        SagaEvent.objects.create(
            transaction_id=txn_pk,
            seq=next_seq,
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            http_status_code=http_status_code,
            http_payload=_mask_pii(http_payload),
        )

    # Database transaction committed. Publish real-time event to Redis for SSE streaming.
    _publish_transaction_update(transaction_id, to_status, from_status)

    return True
