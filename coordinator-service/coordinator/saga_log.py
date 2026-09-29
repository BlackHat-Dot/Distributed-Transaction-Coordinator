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

        Transaction.objects.filter(pk=txn_pk).update(
            status=to_status,
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
            http_payload=http_payload,
        )

    # Database transaction committed. Publish real-time event to Redis for SSE streaming.
    _publish_transaction_update(transaction_id, to_status, from_status)

    return True
