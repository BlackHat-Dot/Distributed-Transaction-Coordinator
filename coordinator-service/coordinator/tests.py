import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from .models import Transaction

URL = "/transactions/"


def _charged_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"status": "CHARGED"}
    return mock


def _reserve_ok_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"message": "Inventory reserved"}
    return mock


def _mock_saga_ok():
    return _SagaOkPatch()


class _SagaOkPatch:
    def __enter__(self):
        self._post_patcher = patch("coordinator.views.requests.post")
        self._get_patcher = patch("coordinator.views.requests.get")
        self.post_mock = self._post_patcher.start()
        self.get_mock = self._get_patcher.start()
        self.post_mock.side_effect = [_reserve_ok_response(), _charged_response()]
        return self.post_mock, self.get_mock

    def __exit__(self, *args):
        self._post_patcher.stop()
        self._get_patcher.stop()


class TransactionIdGenerationTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_no_transaction_id_generates_uuid_and_runs_saga(self):
        with _mock_saga_ok() as (post_mock, _):
            resp = self.client.post(URL, {}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Transaction.Status.COMPLETED)
        tid = resp.data["transaction_id"]
        uuid.UUID(str(tid))
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(post_mock.call_count, 2)

    def test_client_supplied_transaction_id_is_used_verbatim(self):
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
    def setUp(self):
        self.client = APIClient()

    def test_sequential_duplicate_returns_existing_state_no_downstream_calls(self):
        tid = str(uuid.uuid4())

        with _mock_saga_ok() as (post_mock, _):
            resp1 = self.client.post(
                URL, {"transaction_id": tid}, format="json"
            )
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.data["status"], Transaction.Status.COMPLETED)
        first_downstream_calls = post_mock.call_count

        with patch("coordinator.views.requests.post") as post_mock2:
            resp2 = self.client.post(
                URL, {"transaction_id": tid}, format="json"
            )

        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data["status"], Transaction.Status.COMPLETED)
        self.assertEqual(resp2.data["transaction_id"], tid)

        post_mock2.assert_not_called()

        self.assertEqual(
            Transaction.objects.filter(transaction_id=tid).count(), 1
        )

    def test_duplicate_different_amount_returns_original(self):

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


        self.assertEqual(
            str(Transaction.objects.get(transaction_id=tid).amount),
            original_amount,
        )


class TerminalStateTests(TestCase):
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
    def test_concurrent_duplicate_one_row_one_saga(self):
        import threading
        tid = str(uuid.uuid4())
        num_threads = 5
        results = []
        saga_execution_counts = []
        thread_calls = threading.local()

        def mock_post(url, *args, **kwargs):
            thread_calls.count = getattr(thread_calls, 'count', 0) + 1
            if "reserve" in url:
                return _reserve_ok_response()
            if "charge" in url:
                return _charged_response()
            return _reserve_ok_response()

        def do_create(_):
            client = APIClient()
            thread_calls.count = 0
            try:
                resp = client.post(
                    URL,
                    {"transaction_id": tid},
                    format="json",
                )
                return resp.status_code, resp.data, getattr(thread_calls, 'count', 0)
            finally:
                connection.close()

        with patch("coordinator.views.requests.post", side_effect=mock_post):
            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                futures = [executor.submit(do_create, i) for i in range(num_threads)]
                for future in as_completed(futures):
                    status_code, data, downstream_calls = future.result()
                    results.append((status_code, data))
                    saga_execution_counts.append(downstream_calls)

        for code, _ in results:
            self.assertEqual(
                code,
                200,
                f"Thread returned unexpected status {code} — "
                f"possible uncaught IntegrityError",
            )

        statuses = {data["status"] for _, data in results}
        valid_statuses = {s for s, _ in Transaction.Status.choices}
        for s in statuses:
            self.assertIn(s, valid_statuses)

        self.assertEqual(
            Transaction.objects.filter(transaction_id=tid).count(),
            1,
            "More than one Transaction row was created — idempotency failed",
        )

        threads_that_ran_saga = [c for c in saga_execution_counts if c > 0]
        self.assertEqual(
            len(threads_that_ran_saga),
            1,
            f"Expected exactly 1 saga execution but got: {saga_execution_counts}",
        )
