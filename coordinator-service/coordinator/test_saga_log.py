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


class SagaEventModelConstraintTests(TestCase):

    def test_duplicate_seq_raises_integrity_error(self):

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
                seq=1,
                from_status=Transaction.Status.STARTED,
                to_status=Transaction.Status.PAYMENT_PROCESSING,
                actor=SagaEvent.ACTOR_RECOVERY,
            )

    def test_different_seq_same_transaction_is_fine(self):
    
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
        from rest_framework.test import APIClient
        txn = _make_transaction()
        client = APIClient()

        resp = client.get(f"/transactions/{txn.transaction_id}/events/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(str(resp.data["transaction_id"]), str(txn.transaction_id))
        self.assertEqual(resp.data["status"], Transaction.Status.STARTED)
        self.assertEqual(resp.data["events"], [])

class SetStatusWithEventHappyPathTests(TestCase):

    def test_happy_path_status_updated_event_created(self):

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


class SetStatusWithEventCasMissTests(TestCase):

    def test_wrong_expected_status_returns_false_no_writes(self):

        txn = _make_transaction(Transaction.Status.STARTED)

        result = _set_status_with_event(
            transaction_id=txn.transaction_id,
            to_status=Transaction.Status.COMPENSATED,
            expected_status=Transaction.Status.PAYMENT_PROCESSING,
        )

        self.assertFalse(result)

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.STARTED)

        self.assertEqual(SagaEvent.objects.filter(transaction=txn).count(), 0)

    def test_no_expected_status_always_succeeds(self):
    
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
    
        result = _set_status_with_event(
            transaction_id=uuid.uuid4(),
            to_status=Transaction.Status.COMPLETED,
        )
        self.assertFalse(result)
        self.assertEqual(SagaEvent.objects.count(), 0)


class SetStatusWithEventConcurrencyTests(TransactionTestCase):

    def test_concurrent_same_expected_status_exactly_one_wins(self):
    
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

        
        self.assertEqual(
            errors, [],
            f"Unexpected exceptions in threads: {errors}",
        )

    
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

    
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.INVENTORY_RESERVED)


class SagaEventAtomicRollbackTests(TransactionTestCase):
    def test_status_update_rolls_back_if_event_creation_fails(self):
        from unittest.mock import patch
        txn = _make_transaction(Transaction.Status.STARTED)
        tid = txn.transaction_id

        with patch.object(SagaEvent.objects, "create", side_effect=IntegrityError("Forced DB constraint failure")):
            with self.assertRaises(IntegrityError):
                _set_status_with_event(
                    transaction_id=tid,
                    to_status=Transaction.Status.INVENTORY_RESERVED,
                    expected_status=Transaction.Status.STARTED,
                    actor=SagaEvent.ACTOR_REQUEST,
                )

        txn.refresh_from_db()
        self.assertEqual(
            txn.status,
            Transaction.Status.STARTED,
            "Transaction status was modified despite SagaEvent.create failure — atomicity violated!",
        )

        self.assertEqual(SagaEvent.objects.filter(transaction=txn).count(), 0)
