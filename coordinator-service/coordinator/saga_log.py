from django.db import transaction as db_transaction
from django.db.models import Max
from django.utils import timezone

from .models import Transaction, SagaEvent


def _set_status_with_event(
    transaction_id,
    to_status,
    expected_status=None,
    actor=SagaEvent.ACTOR_REQUEST,
    http_status_code=None,
    http_payload=None,
):
    """
    Atomically transitions a Transaction's status and appends an immutable SagaEvent audit record.

    Atomicity & Rollback Guarantee:
    The Transaction status update and the SagaEvent creation are strictly encapsulated within
    the same `db_transaction.atomic()` block under an exclusive `select_for_update()` row lock.
    If `SagaEvent.objects.create()` fails for any reason (e.g. unique constraint violation on
    (transaction, seq), database integrity error, or any unhandled exception) after the Transaction
    status update executes, the entire atomic block is rolled back by the database engine.
    The Transaction's status in the database remains strictly at its pre-transition value,
    guaranteeing that the materialized `Transaction.status` column and the append-only `SagaEvent`
    audit trail never diverge or enter an inconsistent partial state.
    """
    with db_transaction.atomic():
        qs = Transaction.objects.select_for_update().filter(
            transaction_id=transaction_id
        )
        if expected_status is not None:
            if isinstance(expected_status, (list, tuple, set)):
                qs = qs.filter(status__in=expected_status)
            else:
                qs = qs.filter(status=expected_status)

        rows = list(qs.values("id", "status"))
        if not rows:
            return False

        txn_pk = rows[0]["id"]
        from_status = rows[0]["status"]

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
            http_payload=http_payload,
        )

    return True
