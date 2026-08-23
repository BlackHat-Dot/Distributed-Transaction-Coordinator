"""
saga_log.py — Stage 2: atomic CAS + event-append helper.

This module is intentionally NOT imported by views.py or the recovery
command yet. It is tested in isolation first (Stage 2). Wiring it into
existing call sites is Stage 3.
"""

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
    Atomically transition a Transaction status and append a SagaEvent row.

    Returns True if the transition was applied, False if the CAS filter
    matched zero rows (either wrong expected_status, or the row does not exist).

    Locking and ordering rationale
    --------------------------------
    1. WHY select_for_update() MUST be first inside atomic():
       seq is computed as MAX(seq)+1 for this transaction. If two concurrent
       writers both enter the block before either holds the lock, they will both
       compute the same MAX(seq) and attempt to insert seq=N. The
       unique_together constraint will reject the second one, raising
       IntegrityError -- which, inside atomic(), rolls back the entire block,
       including the status update that already succeeded. The transition is
       silently lost.

       By acquiring SELECT FOR UPDATE on the Transaction row as the very first
       statement, we serialize all concurrent writers for the same
       transaction_id at the DB level. Only one writer can be executing inside
       the block at any point in time for a given transaction row. MAX(seq)+1
       is then safe because no other writer can interleave.

    2. WHY status write happens BEFORE event insert (not the reverse):
       The natural instinct is to insert the event first ("record intent before
       acting"). That is wrong. If the process crashes after inserting the
       event but before updating the status column, the DB contains an event
       record for a transition that never actually happened -- the status column
       still shows the old state. This is worse than losing the event: it is
       false history.

       By writing the status first and the event second (both inside the same
       atomic() block), a crash before commit rolls back both. A crash after
       commit means both are durable. There is no window where the event exists
       without the corresponding status change.
    """
    with db_transaction.atomic():
        # Step 1: Acquire exclusive row lock. This MUST be the first
        # statement in the atomic block. No other writer can enter this
        # block for the same transaction_id until we commit or roll back.
        qs = Transaction.objects.select_for_update().filter(
            transaction_id=transaction_id
        )
        if expected_status is not None:
            qs = qs.filter(status=expected_status)

        # Materialize -- this executes the SELECT FOR UPDATE and tells us
        # whether the CAS predicate was satisfied.
        rows = list(qs.values("id", "status"))
        if not rows:
            # CAS miss: either wrong expected_status or row does not exist.
            return False

        txn_pk = rows[0]["id"]
        from_status = rows[0]["status"]

        # Step 2: Write the new status. We are inside the row lock; no
        # concurrent writer can observe this until we commit.
        Transaction.objects.filter(pk=txn_pk).update(
            status=to_status,
            updated_at=timezone.now(),
        )

        # Step 3: Compute the next sequence number. This is safe because
        # the SELECT FOR UPDATE above serializes all writers for this
        # transaction -- no two writers can reach this line simultaneously
        # for the same transaction_id.
        last_seq = (
            SagaEvent.objects.filter(transaction_id=txn_pk)
            .aggregate(m=Max("seq"))["m"]
            or 0
        )
        next_seq = last_seq + 1

        # Step 4: Append the event record. This is the permanent record of
        # what happened. It is written after the status update (not before)
        # for the reason explained in the module docstring.
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
