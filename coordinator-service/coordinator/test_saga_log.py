import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db import connection, IntegrityError
from django.test import TestCase, TransactionTestCase

from coordinator.models import Transaction, SagaEvent
from coordinator.saga_log import _set_status_with_event


def _make_transaction(status=Transaction.Status.STARTED):
    return Transaction.objects.create(
        transaction_id=uuid.uuid4(),
        status=status,
        product_id=1,
        quantity=1,
        amount=100,
    )


# ---------------------------------------------------------------------------
# Stage 1 Tests: SagaEvent model constraints
# ---------------------------------------------------------------------------

class SagaEventModelConstraintTests(TestCase):
    """
    Stage 1: Verify that the unique_together (transaction, seq) constraint
    is enforced at the database level with IntegrityError.
    """

    def test_duplicate_seq_raises_integrity_error(self):
        """
        Two SagaEvent rows with the same (transaction, seq) must raise
        IntegrityError. This is the DB-level guard against seq collisions
        if the SELECT FOR UPDATE logic is ever bypassed.
        """
        txn = _make_transaction()

        SagaEvent.objects.create(
            transaction=txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.INVENTORY_RESERVED,
            actor=SagaEvent.ACTOR_REQUEST,
        )

        with self.assertRaises(IntegrityError):
            SagaEvent.objects.create(
                transaction=txn,
                seq=1,          # same seq — must fail
                from_status=Transaction.Status.STARTED,
                to_status=Transaction.Status.PAYMENT_PROCESSING,
                actor=SagaEvent.ACTOR_RECOVERY,
            )

    def test_different_seq_same_transaction_is_fine(self):
        """Sanity: seq=1 and seq=2 for the same transaction must not conflict."""
        txn = _make_transaction()

        SagaEvent.objects.create(
            transaction=txn, seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.INVENTORY_RESERVED,
            actor=SagaEvent.ACTOR_REQUEST,
        )
        SagaEvent.objects.create(
            transaction=txn, seq=2,
            from_status=Transaction.Status.INVENTORY_RESERVED,
            to_status=Transaction.Status.COMPLETED,
            actor=SagaEvent.ACTOR_REQUEST,
        )
        self.assertEqual(SagaEvent.objects.filter(transaction=txn).count(), 2)

    def test_same_seq_different_transactions_is_fine(self):
        """seq=1 for two different transactions must not conflict."""
        txn1 = _make_transaction()
        txn2 = _make_transaction()

        SagaEvent.objects.create(
            transaction=txn1, seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.INVENTORY_RESERVED,
            actor=SagaEvent.ACTOR_REQUEST,
        )
        SagaEvent.objects.create(
            transaction=txn2, seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.INVENTORY_RESERVED,
            actor=SagaEvent.ACTOR_REQUEST,
        )
        self.assertEqual(SagaEvent.objects.count(), 2)

    def test_events_endpoint_returns_empty_list_for_new_transaction(self):
        """
        Stage 1 read endpoint: GET /transactions/{id}/events/ returns an
        empty events list before any _set_status_with_event calls are made.
        """
        from rest_framework.test import APIClient
        txn = _make_transaction()
        client = APIClient()

        resp = client.get(f"/transactions/{txn.transaction_id}/events/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(str(resp.data["transaction_id"]), str(txn.transaction_id))
        self.assertEqual(resp.data["status"], Transaction.Status.STARTED)
        self.assertEqual(resp.data["events"], [])


# ---------------------------------------------------------------------------
# Stage 2 Test 1: Happy path
# ---------------------------------------------------------------------------

class SetStatusWithEventHappyPathTests(TestCase):

    def test_happy_path_status_updated_event_created(self):
        """
        Happy path: calling _set_status_with_event on a STARTED transaction
        with expected_status=STARTED must:
        - return True
        - update Transaction.status to the new value
        - create exactly one SagaEvent row with correct fields and seq=1
        """
        txn = _make_transaction(Transaction.Status.STARTED)

        result = _set_status_with_event(
            transaction_id=txn.transaction_id,
            to_status=Transaction.Status.INVENTORY_RESERVED,
            expected_status=Transaction.Status.STARTED,
            actor=SagaEvent.ACTOR_REQUEST,
            http_status_code=200,
            http_payload={"message": "Inventory reserved"},
        )

        self.assertTrue(result)

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.INVENTORY_RESERVED)

        events = SagaEvent.objects.filter(transaction=txn)
        self.assertEqual(events.count(), 1)

        event = events.first()
        self.assertEqual(event.seq, 1)
        self.assertEqual(event.from_status, Transaction.Status.STARTED)
        self.assertEqual(event.to_status, Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(event.actor, SagaEvent.ACTOR_REQUEST)
        self.assertEqual(event.http_status_code, 200)
        self.assertEqual(event.http_payload, {"message": "Inventory reserved"})

    def test_sequential_calls_produce_incrementing_seq(self):
        """Two sequential transitions must produce seq=1 then seq=2."""
        txn = _make_transaction(Transaction.Status.STARTED)

        _set_status_with_event(
            txn.transaction_id,
            Transaction.Status.INVENTORY_RESERVED,
            expected_status=Transaction.Status.STARTED,
        )
        _set_status_with_event(
            txn.transaction_id,
            Transaction.Status.PAYMENT_PROCESSING,
            expected_status=Transaction.Status.INVENTORY_RESERVED,
        )

        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[1].seq, 2)
        self.assertEqual(events[1].from_status, Transaction.Status.INVENTORY_RESERVED)
        self.assertEqual(events[1].to_status, Transaction.Status.PAYMENT_PROCESSING)


# ---------------------------------------------------------------------------
# Stage 2 Test 2: CAS miss
# ---------------------------------------------------------------------------

class SetStatusWithEventCasMissTests(TestCase):

    def test_wrong_expected_status_returns_false_no_writes(self):
        """
        CAS miss: calling with expected_status that does not match the
        current Transaction.status must:
        - return False
        - leave Transaction.status unchanged
        - create zero SagaEvent rows
        """
        txn = _make_transaction(Transaction.Status.STARTED)

        result = _set_status_with_event(
            transaction_id=txn.transaction_id,
            to_status=Transaction.Status.COMPENSATED,
            expected_status=Transaction.Status.PAYMENT_PROCESSING,  # wrong
        )

        self.assertFalse(result)

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.STARTED)  # unchanged

        self.assertEqual(SagaEvent.objects.filter(transaction=txn).count(), 0)

    def test_no_expected_status_always_succeeds(self):
        """Omitting expected_status is an unconditional transition (no CAS)."""
        txn = _make_transaction(Transaction.Status.STARTED)

        result = _set_status_with_event(
            transaction_id=txn.transaction_id,
            to_status=Transaction.Status.COMPENSATED,
            expected_status=None,
        )

        self.assertTrue(result)
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED)
        self.assertEqual(SagaEvent.objects.filter(transaction=txn).count(), 1)

    def test_nonexistent_transaction_id_returns_false(self):
        """Calling with a UUID that has no Transaction row must return False."""
        result = _set_status_with_event(
            transaction_id=uuid.uuid4(),
            to_status=Transaction.Status.COMPLETED,
        )
        self.assertFalse(result)
        self.assertEqual(SagaEvent.objects.count(), 0)


# ---------------------------------------------------------------------------
# Stage 2 Test 3: Concurrency — TransactionTestCase with real commits
# ---------------------------------------------------------------------------

class SetStatusWithEventConcurrencyTests(TransactionTestCase):
    """
    Uses TransactionTestCase (not TestCase) so that each thread operates
    in a real separate DB transaction that actually commits. Django''s
    TestCase wraps everything in a single non-committed transaction, which
    makes SELECT FOR UPDATE meaningless because no two threads can block
    each other on the same transaction-level lock.
    """

    def test_concurrent_same_expected_status_exactly_one_wins(self):
        """
        Two threads call _set_status_with_event simultaneously on the same
        transaction row with the same expected_status=STARTED.

        Expected outcome:
        - Exactly one call returns True (won the lock, committed).
        - The other call returns False (lost the CAS after the winner
          changed the status, so its SELECT FOR UPDATE sees the new status
          and the expected_status filter returns 0 rows).
        - Exactly one SagaEvent row exists with seq=1.
        - No seq collision (no IntegrityError), no duplicate event.
        """
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.STARTED,
            product_id=1,
            quantity=1,
            amount=100,
        )
        tid = txn.transaction_id

        results = []
        errors = []

        def do_transition(_):
            try:
                outcome = _set_status_with_event(
                    transaction_id=tid,
                    to_status=Transaction.Status.INVENTORY_RESERVED,
                    expected_status=Transaction.Status.STARTED,
                    actor=SagaEvent.ACTOR_REQUEST,
                )
                return outcome
            except Exception as exc:
                errors.append(exc)
                return None
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(do_transition, i) for i in range(2)]
            for f in as_completed(futures):
                results.append(f.result())

        # No exceptions should have escaped
        self.assertEqual(
            errors, [],
            f"Unexpected exceptions in threads: {errors}",
        )

        # Exactly one winner
        self.assertEqual(
            results.count(True),
            1,
            f"Expected exactly 1 True but got results={results}. "
            "SELECT FOR UPDATE is not serializing the writers correctly.",
        )
        self.assertEqual(
            results.count(False),
            1,
            f"Expected exactly 1 False but got results={results}.",
        )

        # Exactly one event row, seq=1, no duplicates
        events = list(SagaEvent.objects.filter(transaction_id=txn.pk).order_by("seq"))
        self.assertEqual(
            len(events),
            1,
            f"Expected exactly 1 SagaEvent but found {len(events)}. "
            "Possible seq collision or duplicate event insert.",
        )
        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[0].from_status, Transaction.Status.STARTED)
        self.assertEqual(events[0].to_status, Transaction.Status.INVENTORY_RESERVED)

        # Final status must be INVENTORY_RESERVED (the winner committed it)
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.INVENTORY_RESERVED)
