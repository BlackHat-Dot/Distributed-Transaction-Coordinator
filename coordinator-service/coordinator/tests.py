"""
Coordinator idempotency tests.

All outbound HTTP calls to Inventory (/products/reserve/, /products/release/)
and Payment (/payments/charge/, /payments/check/) are mocked so this suite
runs without those services being up.

The concurrent test (ConcurrentCreateTests) runs against PostgreSQL — the same
backend configured in settings.py (django.db.backends.postgresql, DB:
dtc_coordinator). There is no TEST: override in settings.py redirecting to
SQLite; Django will create test_dtc_coordinator on the running Postgres
instance. The test therefore exercises the real unique constraint race.
"""
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from .models import Transaction


# Helpers

URL = "/transactions/"


def _charged_response():
    """Mock payment response body for a successful charge."""
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"status": "CHARGED"}
    return mock


def _reserve_ok_response():
    """Mock Inventory reserve success response."""
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"message": "Inventory reserved"}
    return mock


def _mock_saga_ok():
    """
    Context manager stack: patches both Inventory reserve and Payment charge
    to succeed. Returns (reserve_mock, payment_mock) for assertion.

    Usage:
        with _mock_saga_ok() as (reserve, charge):
            resp = client.post(...)
            reserve.assert_called_once()
    """
    return _SagaOkPatch()


class _SagaOkPatch:
    """Patches requests.post so the first call (Inventory) returns 200 and
    the second call (Payment) returns CHARGED. Patches requests.get for the
    payment check endpoint (not reached in normal flow but included for
    completeness)."""

    def __enter__(self):
        self._post_patcher = patch("coordinator.views.requests.post")
        self._get_patcher = patch("coordinator.views.requests.get")
        self.post_mock = self._post_patcher.start()
        self.get_mock = self._get_patcher.start()
        # side_effect list: first call = Inventory, second = Payment
        self.post_mock.side_effect = [_reserve_ok_response(), _charged_response()]
        return self.post_mock, self.get_mock

    def __exit__(self, *args):
        self._post_patcher.stop()
        self._get_patcher.stop()


# ── Test classes ──────────────────────────────────────────────────────────────


class TransactionIdGenerationTests(TestCase):
    """
    Test 1: No transaction_id supplied → UUID generated server-side.
    Test 2: Valid client-supplied transaction_id → used verbatim.
    Test 3: Invalid transaction_id → 400, no DB row, no downstream calls.
    """

    def setUp(self):
        self.client = APIClient()

    def test_no_transaction_id_generates_uuid_and_runs_saga(self):
        """Omitting transaction_id preserves the original behaviour: a fresh
        UUID is generated and the saga executes normally."""
        with _mock_saga_ok() as (post_mock, _):
            resp = self.client.post(URL, {}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Transaction.Status.COMPLETED)
        tid = resp.data["transaction_id"]
        # Must be a valid UUID string
        uuid.UUID(str(tid))  # must be parseable as a UUID
        # Exactly one Transaction row, one Inventory call, one Payment call
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(post_mock.call_count, 2)  # reserve + charge

    def test_client_supplied_transaction_id_is_used_verbatim(self):
        """A valid UUID supplied in the request body must be stored unchanged."""
        tid = str(uuid.uuid4())
        with _mock_saga_ok():
            resp = self.client.post(
                URL, {"transaction_id": tid}, format="json"
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(str(resp.data["transaction_id"]), tid)
        self.assertTrue(
            Transaction.objects.filter(transaction_id=tid).exists()
        )

    def test_invalid_transaction_id_returns_400(self):
        """A non-UUID string must be rejected immediately with 400.
        No Transaction row must be created. No downstream calls made."""
        with patch("coordinator.views.requests.post") as post_mock:
            resp = self.client.post(
                URL,
                {"transaction_id": "not-a-uuid"},
                format="json",
            )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("transaction_id", resp.data.get("error", ""))
        self.assertEqual(Transaction.objects.count(), 0)
        post_mock.assert_not_called()


class SequentialDuplicateTests(TestCase):
    """
    Test 4: Same transaction_id sent twice sequentially.
    Second call must return the existing row's status without touching
    Inventory or Payment.
    """

    def setUp(self):
        self.client = APIClient()

    def test_sequential_duplicate_returns_existing_state_no_downstream_calls(self):
        tid = str(uuid.uuid4())

        # First request — saga runs, Transaction reaches COMPLETED
        with _mock_saga_ok() as (post_mock, _):
            resp1 = self.client.post(
                URL, {"transaction_id": tid}, format="json"
            )
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.data["status"], Transaction.Status.COMPLETED)
        first_downstream_calls = post_mock.call_count  # should be 2

        # Second request — same tid, saga must NOT run
        with patch("coordinator.views.requests.post") as post_mock2:
            resp2 = self.client.post(
                URL, {"transaction_id": tid}, format="json"
            )

        # Same status returned
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data["status"], Transaction.Status.COMPLETED)
        self.assertEqual(resp2.data["transaction_id"], tid)
        # Zero downstream calls on the duplicate
        post_mock2.assert_not_called()
        # Still only one row
        self.assertEqual(
            Transaction.objects.filter(transaction_id=tid).count(), 1
        )

    def test_duplicate_different_amount_returns_original(self):
        """Idempotency: duplicate ignores the new request body entirely."""
        tid = str(uuid.uuid4())

        with _mock_saga_ok():
            self.client.post(
                URL, {"transaction_id": tid, "amount": 500}, format="json"
            )

        original_amount = str(
            Transaction.objects.get(transaction_id=tid).amount
        )

        with patch("coordinator.views.requests.post"):
            resp2 = self.client.post(
                URL, {"transaction_id": tid, "amount": 9999}, format="json"
            )

        # Amount in the DB must not have changed
        self.assertEqual(
            str(Transaction.objects.get(transaction_id=tid).amount),
            original_amount,
        )


class TerminalStateTests(TestCase):
    """
    Test 6: COMPLETED / COMPENSATED transactions return existing state with
    zero downstream calls when the same transaction_id is re-sent.
    """

    def setUp(self):
        self.client = APIClient()

    def _make_transaction(self, status, tid=None):
        tid = tid or uuid.uuid4()
        return Transaction.objects.create(
            transaction_id=tid,
            status=status,
            product_id=1,
            quantity=1,
            amount=500,
        )

    def test_completed_transaction_returns_existing_no_downstream(self):
        txn = self._make_transaction(Transaction.Status.COMPLETED)

        with patch("coordinator.views.requests.post") as post_mock:
            resp = self.client.post(
                URL,
                {"transaction_id": str(txn.transaction_id)},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Transaction.Status.COMPLETED)
        post_mock.assert_not_called()
        self.assertEqual(
            Transaction.objects.filter(
                transaction_id=txn.transaction_id
            ).count(),
            1,
        )

    def test_compensated_transaction_returns_existing_no_downstream(self):
        txn = self._make_transaction(Transaction.Status.COMPENSATED)

        with patch("coordinator.views.requests.post") as post_mock:
            resp = self.client.post(
                URL,
                {"transaction_id": str(txn.transaction_id)},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Transaction.Status.COMPENSATED)
        post_mock.assert_not_called()

    def test_compensation_failed_returns_existing_no_downstream(self):
        txn = self._make_transaction(Transaction.Status.COMPENSATION_FAILED)

        with patch("coordinator.views.requests.post") as post_mock:
            resp = self.client.post(
                URL,
                {"transaction_id": str(txn.transaction_id)},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.data["status"], Transaction.Status.COMPENSATION_FAILED
        )
        post_mock.assert_not_called()


class InProgressDuplicateTests(TestCase):
    """
    Test 7: Transaction is in a non-terminal state (STARTED, PAYMENT_PROCESSING,
    COMPENSATING). A duplicate request must return 200 with the current status
    and must not start a second saga.
    """

    def setUp(self):
        self.client = APIClient()

    def _make_transaction(self, status):
        return Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=status,
            product_id=1,
            quantity=1,
            amount=500,
        )

    def _assert_idempotent(self, status):
        txn = self._make_transaction(status)

        with patch("coordinator.views.requests.post") as post_mock:
            resp = self.client.post(
                URL,
                {"transaction_id": str(txn.transaction_id)},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], status)
        post_mock.assert_not_called()

    def test_started_no_second_saga(self):
        self._assert_idempotent(Transaction.Status.STARTED)

    def test_payment_processing_no_second_saga(self):
        self._assert_idempotent(Transaction.Status.PAYMENT_PROCESSING)

    def test_compensating_no_second_saga(self):
        self._assert_idempotent(Transaction.Status.COMPENSATING)

    def test_inventory_reserved_no_second_saga(self):
        self._assert_idempotent(Transaction.Status.INVENTORY_RESERVED)


class ConcurrentCreateTests(TransactionTestCase):
    """
    Test 5: Concurrent duplicate transaction_id against real PostgreSQL.

    Uses TransactionTestCase (not TestCase) so each thread gets its own real
    DB transaction — TestCase wraps everything in a single transaction that
    other threads cannot see.

    DB backend: django.db.backends.postgresql (settings.py, no TEST override).
    Django creates test_dtc_coordinator on the running Postgres instance.
    This test exercises the real UNIQUE constraint race — not SQLite behaviour.
    """

    def test_concurrent_duplicate_one_row_one_saga(self):
        tid = str(uuid.uuid4())
        num_threads = 5
        results = []

        # Each thread gets its own patched requests.post that records calls.
        # We count how many threads actually ran the saga (i.e., made
        # downstream calls) — it must be exactly 1.
        saga_execution_counts = []

        def do_create(_):
            client = APIClient()
            call_count_before = [0]
            try:
                with patch("coordinator.views.requests.post") as post_mock:
                    # Inventory: 200 OK; Payment: CHARGED
                    post_mock.side_effect = [
                        _reserve_ok_response(),
                        _charged_response(),
                    ]
                    resp = client.post(
                        URL,
                        {"transaction_id": tid},
                        format="json",
                    )
                    call_count_before[0] = post_mock.call_count
                return resp.status_code, resp.data, call_count_before[0]
            finally:
                # Close the thread's DB connection so Django's test runner
                # can drop test_dtc_coordinator when teardown runs.
                connection.close()

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(do_create, i) for i in range(num_threads)]
            for future in as_completed(futures):
                status_code, data, downstream_calls = future.result()
                results.append((status_code, data))
                saga_execution_counts.append(downstream_calls)

        # ── Assertions ────────────────────────────────────────────────────────

        # All threads must receive a clean response (no 500s)
        for code, _ in results:
            self.assertIn(
                code,
                (200,),
                f"Thread returned unexpected status {code} — "
                f"possible uncaught IntegrityError",
            )

        # All responses must carry a consistent status
        statuses = {data["status"] for _, data in results}
        # The winning thread completes the saga → COMPLETED.
        # Losers re-fetch at some point during or after the saga.
        # All statuses must be valid Transaction statuses (no garbage).
        valid_statuses = {s for s, _ in Transaction.Status.choices}
        for s in statuses:
            self.assertIn(s, valid_statuses)

        # Exactly one Transaction row in the database
        self.assertEqual(
            Transaction.objects.filter(transaction_id=tid).count(),
            1,
            "More than one Transaction row was created — idempotency failed",
        )

        # Exactly one thread ran the saga (made downstream HTTP calls)
        # The rest must have short-circuited with 0 calls.
        threads_that_ran_saga = [c for c in saga_execution_counts if c > 0]
        self.assertEqual(
            len(threads_that_ran_saga),
            1,
            f"Expected exactly 1 saga execution but got: {saga_execution_counts}",
        )
