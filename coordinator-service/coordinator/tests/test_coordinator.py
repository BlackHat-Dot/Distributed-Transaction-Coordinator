import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from coordinator.models import Transaction
from coordinator.views import _create_transaction_idempotent

URL = "/transactions/?sync=true"


def _seat_pricing_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = [{"id": 1, "price": 500, "row": "A", "seat_number": "1"}]
    return mock


def _charged_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"status": "CHARGED"}
    return mock


def _reserve_ok_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"message": "Seat reserved", "seat_ids": [1]}
    return mock


def _confirm_ok_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"message": "Seat confirmed", "seat_ids": [1], "status": "BOOKED"}
    return mock


def _mock_saga_ok():
    return _SagaOkPatch()


class _SagaOkPatch:
    def __enter__(self):
        self._post_patcher = patch("coordinator.views.requests.post")
        self.post_mock = self._post_patcher.start()
        self.post_mock.side_effect = _dispatch_post
        return self.post_mock, None

    def __exit__(self, *args):
        self._post_patcher.stop()


def _dispatch_post(url, *args, **kwargs):
    """Routes POST calls by URL for coordinator tests."""
    mock = MagicMock()
    if "pricing" in url:
        mock.status_code = 200
        mock.json.return_value = [{"id": 1, "price": 500, "row": "A", "seat_number": "1"}]
        return mock
    if "reserve" in url:
        mock.status_code = 200
        mock.json.return_value = {"message": "Seat reserved", "seat_ids": [1]}
        return mock
    if "charge" in url:
        mock.status_code = 200
        mock.json.return_value = {"status": "CHARGED"}
        return mock
    if "confirm" in url:
        mock.status_code = 200
        mock.json.return_value = {"message": "Seat confirmed", "seat_ids": [1], "status": "BOOKED"}
        return mock
    raise ValueError(f"Unexpected POST url in _dispatch_post: {url}")


class TransactionIdGenerationTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_no_transaction_id_generates_uuid_and_runs_saga(self):
        with _mock_saga_ok() as (post_mock, _):
            resp = self.client.post(URL, {"seat_ids": [1]}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Transaction.Status.COMPLETED)
        tid = resp.data["transaction_id"]
        uuid.UUID(str(tid))
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(post_mock.call_count, 4)  # pricing + reserve + charge + confirm


class SequentialDuplicateTests(TestCase):
    def test_sequential_duplicate_returns_existing_state(self):
        tid = str(uuid.uuid4())

        created1, resp1 = _create_transaction_idempotent(
            transaction_id=tid, seat_ids=[1], amount=500
        )
        self.assertTrue(created1)
        self.assertIsNone(resp1)

        txn = Transaction.objects.get(transaction_id=tid)
        txn.status = Transaction.Status.COMPLETED
        txn.save()

        created2, resp2 = _create_transaction_idempotent(
            transaction_id=tid, seat_ids=[1], amount=500
        )
        self.assertFalse(created2)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data["status"], Transaction.Status.COMPLETED)
        self.assertEqual(resp2.data["transaction_id"], tid)
        self.assertEqual(
            Transaction.objects.filter(transaction_id=tid).count(), 1
        )


class TerminalStateTests(TestCase):
    def _make_transaction(self, status, tid=None):
        tid = tid or str(uuid.uuid4())
        return Transaction.objects.create(
            transaction_id=tid,
            status=status,
            seat_ids=[1],
            quantity=1,
            amount=500,
        )

    def test_completed_transaction_returns_existing(self):
        txn = self._make_transaction(Transaction.Status.COMPLETED)
        created, resp = _create_transaction_idempotent(txn.transaction_id)
        self.assertFalse(created)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Transaction.Status.COMPLETED)

    def test_compensated_transaction_returns_existing(self):
        txn = self._make_transaction(Transaction.Status.COMPENSATED)
        created, resp = _create_transaction_idempotent(txn.transaction_id)
        self.assertFalse(created)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Transaction.Status.COMPENSATED)

    def test_compensation_failed_returns_existing(self):
        txn = self._make_transaction(Transaction.Status.COMPENSATION_FAILED)
        created, resp = _create_transaction_idempotent(txn.transaction_id)
        self.assertFalse(created)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Transaction.Status.COMPENSATION_FAILED)


class InProgressDuplicateTests(TestCase):
    def _make_transaction(self, status):
        return Transaction.objects.create(
            transaction_id=str(uuid.uuid4()),
            status=status,
            seat_ids=[1],
            quantity=1,
            amount=500,
        )

    def _assert_idempotent(self, status):
        txn = self._make_transaction(status)
        created, resp = _create_transaction_idempotent(txn.transaction_id)
        self.assertFalse(created)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], status)

    def test_started_no_second_saga(self):
        self._assert_idempotent(Transaction.Status.STARTED)

    def test_payment_processing_no_second_saga(self):
        self._assert_idempotent(Transaction.Status.PAYMENT_PROCESSING)

    def test_inventory_reserved_no_second_saga(self):
        self._assert_idempotent(Transaction.Status.INVENTORY_RESERVED)


class ConcurrentCreateTests(TransactionTestCase):
    def test_concurrent_duplicate_one_row_one_saga(self):
        tid = str(uuid.uuid4())
        num_threads = 5
        results = []

        def do_create(_):
            try:
                created, dup_resp = _create_transaction_idempotent(
                    transaction_id=tid, seat_ids=[1], amount=500
                )
                return created, dup_resp
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(do_create, i) for i in range(num_threads)]
            for future in as_completed(futures):
                created, dup_resp = future.result()
                results.append((created, dup_resp))

        created_count = sum(1 for created, _ in results if created)
        self.assertEqual(created_count, 1, "Exactly one thread should have created the transaction")

        self.assertEqual(
            Transaction.objects.filter(transaction_id=tid).count(),
            1,
            "More than one Transaction row was created — idempotency failed",
        )


class Batch3RefundCompensationTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_compensation_when_payment_was_charged_calls_refund_and_compensates(self):
        captured_calls = []

        def mock_post(url, *args, **kwargs):
            captured_calls.append(url)
            mock = MagicMock()
            if "pricing" in url:
                mock.status_code = 200
                mock.json.return_value = [{"id": 1, "price": 500, "row": "A", "seat_number": "1"}]
                return mock
            if "reserve" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Reserved", "seat_ids": [1]}
                return mock
            if "charge" in url:
                mock.status_code = 200
                mock.json.return_value = {"status": "CHARGED"}
                return mock
            if "confirm" in url:
                mock.status_code = 409
                mock.json.return_value = {"error": "Hold expired", "code": "HOLD_EXPIRED"}
                return mock
            if "release" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat released"}
                return mock
            if "refund" in url:
                mock.status_code = 200
                mock.json.return_value = {"status": "REFUNDED"}
                return mock
            raise ValueError(f"Unexpected URL: {url}")

        with patch("coordinator.views.requests.post", side_effect=mock_post):
            resp = self.client.post("/transactions/?sync=true", {"seat_ids": [1]}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("status"), Transaction.Status.COMPENSATED)
        self.assertIn("refund_response", resp.data)
        self.assertTrue(any("refund" in u for u in captured_calls), "Refund endpoint must be called")
        tid = resp.data.get("transaction_id")
        self.assertIsNotNone(tid)
        txn = Transaction.objects.get(transaction_id=tid)
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED)

    def test_compensation_when_refund_fails_transitions_to_compensation_failed(self):
        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "pricing" in url:
                mock.status_code = 200
                mock.json.return_value = [{"id": 1, "price": 500, "row": "A", "seat_number": "1"}]
                return mock
            if "reserve" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Reserved", "seat_ids": [1]}
                return mock
            if "charge" in url:
                mock.status_code = 200
                mock.json.return_value = {"status": "CHARGED"}
                return mock
            if "confirm" in url:
                mock.status_code = 409
                mock.json.return_value = {"error": "Hold expired", "code": "HOLD_EXPIRED"}
                return mock
            if "release" in url:
                mock.status_code = 200
                mock.json.return_value = {"message": "Seat released"}
                return mock
            if "refund" in url:
                mock.status_code = 500
                mock.json.return_value = {"error": "Payment gateway down"}
                return mock
            raise ValueError(f"Unexpected URL: {url}")

        with patch("coordinator.views.requests.post", side_effect=mock_post):
            resp = self.client.post("/transactions/?sync=true", {"seat_ids": [1]}, format="json")

        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.data.get("status"), Transaction.Status.COMPENSATION_FAILED)
        tid = resp.data.get("transaction_id")
        self.assertIsNotNone(tid)
        txn = Transaction.objects.get(transaction_id=tid)
        self.assertEqual(txn.status, Transaction.Status.COMPENSATION_FAILED)


class StrictPayloadValidationTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_client_supplied_transaction_id_rejected(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            "/transactions/?sync=true",
            {"transaction_id": tid, "seat_ids": [1]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_FIELDS")
        self.assertIn("transaction_id", resp.data.get("invalid_fields", []))
        self.assertIn("transaction_id", resp.data.get("error", ""))

    def test_client_supplied_amount_rejected(self):
        resp = self.client.post(
            "/transactions/?sync=true",
            {"amount": 500, "seat_ids": [1]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_FIELDS")
        self.assertIn("amount", resp.data.get("invalid_fields", []))
        self.assertIn("amount", resp.data.get("error", ""))

    def test_client_supplied_identity_and_quantity_fields_rejected(self):
        resp = self.client.post(
            "/transactions/?sync=true",
            {
                "user_id": "usr_hacker",
                "user_email": "hacker@evil.com",
                "quantity": 1,
                "seat_ids": [1],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_FIELDS")
        for field in ["user_id", "user_email", "quantity"]:
            self.assertIn(field, resp.data.get("invalid_fields", []))

    def test_arbitrary_extra_fields_rejected_with_explicit_prompt(self):
        resp = self.client.post(
            "/transactions/?sync=true",
            {"custom_discount": 100, "seat_ids": [1]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_FIELDS")
        self.assertIn("custom_discount", resp.data.get("invalid_fields", []))
        self.assertIn("custom_discount", resp.data.get("error", ""))

    def test_valid_payload_generates_backend_uuid_and_succeeds(self):
        with _mock_saga_ok():
            resp = self.client.post(
                "/transactions/?sync=true",
                {"seat_ids": [1]},
                format="json",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), Transaction.Status.COMPLETED)
        generated_id = resp.data.get("transaction_id")
        self.assertIsNotNone(generated_id)
        # Verify it is a valid UUID
        uuid.UUID(str(generated_id))
        # Verify row was created with server-generated ID
        self.assertTrue(Transaction.objects.filter(transaction_id=generated_id).exists())

    def test_legacy_seat_id_and_seat_numbers_rejected(self):
        resp = self.client.post(
            "/transactions/?sync=true",
            {"seat_id": 1, "seat_numbers": "A1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_FIELDS")
        self.assertIn("seat_id", resp.data.get("invalid_fields", []))
        self.assertIn("seat_numbers", resp.data.get("invalid_fields", []))


class TwoPhaseHoldAndPayTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_two_phase_hold_and_pay_flow(self):
        # 1. HOLD phase
        with _mock_saga_ok() as (mock_post, _):
            hold_resp = self.client.post(
                "/transactions/?sync=true",
                {"action": "hold", "seat_ids": [1]},
                format="json",
            )
        self.assertEqual(hold_resp.status_code, 200)
        self.assertEqual(hold_resp.data.get("status"), Transaction.Status.HOLDING)
        tid = hold_resp.data.get("transaction_id")
        self.assertIsNotNone(tid)

        # Verify DB status is HOLDING
        txn = Transaction.objects.get(transaction_id=tid)
        self.assertEqual(txn.status, Transaction.Status.HOLDING)

        # 2. PAY phase
        with _mock_saga_ok() as (mock_post, _):
            pay_resp = self.client.post(
                "/transactions/?sync=true",
                {"action": "pay", "transaction_id": tid},
                format="json",
            )
        self.assertEqual(pay_resp.status_code, 200)
        self.assertEqual(pay_resp.data.get("status"), Transaction.Status.COMPLETED)

        # Verify DB status is COMPLETED
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPLETED)

        # Verify reserve was NOT called during the pay phase!
        called_urls = [call.args[0] for call in mock_post.call_args_list]
        self.assertFalse(any("reserve" in url for url in called_urls), "reserve/ must NOT be called during pay phase")
        self.assertTrue(any("charge" in url for url in called_urls), "charge/ must be called during pay phase")
        self.assertTrue(any("confirm" in url for url in called_urls), "confirm/ must be called during pay phase")

    def test_hold_rejects_client_transaction_id(self):
        resp = self.client.post(
            "/transactions/?sync=true",
            {"action": "hold", "seat_ids": [1], "transaction_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_FIELDS")

    def test_pay_requires_transaction_id(self):
        resp = self.client.post(
            "/transactions/?sync=true",
            {"action": "pay"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "TRANSACTION_ID_REQUIRED")

    def test_pay_rejects_invalid_uuid(self):
        resp = self.client.post(
            "/transactions/?sync=true",
            {"action": "pay", "transaction_id": "not-a-uuid"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_TRANSACTION_ID")

    def test_pay_nonexistent_transaction_returns_404(self):
        resp = self.client.post(
            "/transactions/?sync=true",
            {"action": "pay", "transaction_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data.get("code"), "TRANSACTION_NOT_FOUND")

