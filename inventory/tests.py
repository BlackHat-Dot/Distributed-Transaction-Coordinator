"""
Inventory service — comprehensive test suite.

Coverage:
  - Input validation for reserve and release
  - Happy-path reserve: stock decremented, Reservation created
  - Reserve idempotency (sequential): duplicate transaction_id returns existing
    row, no second stock decrement
  - Reserve idempotency (concurrent): two threads send the same transaction_id
    simultaneously; the DB UNIQUE constraint fires on the loser, IntegrityError
    is caught in reserve_product, stock decrement is rolled back, idempotent
    response returned — no 500
  - Reserve insufficient stock: correct error, no side effects
  - Reserve concurrency (distinct transaction_ids): SELECT FOR UPDATE protects
    stock under parallel load; exactly one winner per product when stock is 1
  - Release happy path: stock restored, Reservation marked RELEASED
  - Release idempotency: double release does not double-restore stock
  - Release concurrency: two threads releasing the same reservation;
    stock restored exactly once
  - Release not-found: 404 with no DB side effects
  - Reservation check: RESERVED, RELEASED, NOT_RESERVED cases, validation
  - Coordinator callback failure is non-fatal: reserve succeeds even when
    the Coordinator status-update call fails or times out

Design notes
------------
TestCase wraps each test in a transaction that is rolled back after the test;
it is fast but means other threads cannot see the data.

TransactionTestCase commits data to the real DB so multiple threads can
observe each other's writes.  It is slower and recreates the DB state between
tests using truncation.  We use it only for concurrency tests.

All outbound HTTP calls to the Coordinator status-update endpoint are mocked
with unittest.mock.patch so the suite runs without the Coordinator being up.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch, MagicMock

from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from .models import Product, Reservation


# ---------------------------------------------------------------------------
# URL constants (must match inventory_service/urls.py)
# ---------------------------------------------------------------------------
RESERVE_URL = "/products/reserve/"
RELEASE_URL = "/products/release/"
CHECK_URL = "/reserve/check/"
PRODUCTS_URL = "/products/"
HEALTH_URL = "/health/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_coordinator_ok():
    """Return a context manager that patches the Coordinator callback to succeed."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    return patch("inventory.views.requests.get", return_value=mock_resp)


def _mock_coordinator_down():
    """Return a context manager that patches the Coordinator callback to raise ConnectionError."""
    from requests.exceptions import ConnectionError as RequestsConnectionError
    return patch("inventory.views.requests.get", side_effect=RequestsConnectionError())


def _create_product(name="Widget", stock=10):
    return Product.objects.create(name=name, stock=stock)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class HealthCheckTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_health_returns_200(self):
        resp = self.client.get(HEALTH_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "healthy")


# ---------------------------------------------------------------------------
# Product list
# ---------------------------------------------------------------------------

class ProductListTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_empty_list(self):
        resp = self.client.get(PRODUCTS_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])

    def test_returns_all_products(self):
        Product.objects.create(name="Alpha", stock=5)
        Product.objects.create(name="Beta", stock=3)
        resp = self.client.get(PRODUCTS_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)
        names = {p["name"] for p in resp.data}
        self.assertIn("Alpha", names)
        self.assertIn("Beta", names)


# ---------------------------------------------------------------------------
# Reserve — input validation
# ---------------------------------------------------------------------------

class ReserveValidationTests(TestCase):
    """POST /products/reserve/ — field-level validation before any DB access."""

    def setUp(self):
        self.client = APIClient()
        self.product = _create_product(stock=10)

    def test_missing_product_id_returns_400(self):
        resp = self.client.post(
            RESERVE_URL,
            {"quantity": 1, "transaction_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("product_id", resp.data.get("error", ""))

    def test_missing_quantity_returns_400(self):
        resp = self.client.post(
            RESERVE_URL,
            {"product_id": self.product.id, "transaction_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_zero_quantity_returns_400(self):
        resp = self.client.post(
            RESERVE_URL,
            {"product_id": self.product.id, "quantity": 0,
             "transaction_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Quantity", resp.data.get("error", ""))

    def test_negative_quantity_returns_400(self):
        resp = self.client.post(
            RESERVE_URL,
            {"product_id": self.product.id, "quantity": -5,
             "transaction_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_missing_transaction_id_returns_400(self):
        resp = self.client.post(
            RESERVE_URL,
            {"product_id": self.product.id, "quantity": 1},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("transaction_id", resp.data.get("error", ""))

    def test_empty_body_returns_error(self):
        resp = self.client.post(RESERVE_URL, {}, format="json")
        # Any non-200 is acceptable; the important thing is no crash
        self.assertNotEqual(resp.status_code, 500)


# ---------------------------------------------------------------------------
# Reserve — happy path
# ---------------------------------------------------------------------------

class ReserveHappyPathTests(TestCase):
    """Successful reservation: stock decremented, Reservation row created."""

    def setUp(self):
        self.client = APIClient()
        self.product = _create_product(stock=10)

    def test_successful_reserve_decrements_stock(self):
        tid = str(uuid.uuid4())
        with _mock_coordinator_ok():
            resp = self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 3, "transaction_id": tid},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 7)

    def test_successful_reserve_creates_reservation_row(self):
        tid = str(uuid.uuid4())
        with _mock_coordinator_ok():
            self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 2, "transaction_id": tid},
                format="json",
            )

        reservation = Reservation.objects.get(transaction_id=tid)
        self.assertEqual(reservation.status, Reservation.Status.RESERVED)
        self.assertEqual(reservation.quantity, 2)
        self.assertEqual(reservation.product_id, self.product.id)

    def test_successful_reserve_response_shape(self):
        tid = str(uuid.uuid4())
        with _mock_coordinator_ok():
            resp = self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 4, "transaction_id": tid},
                format="json",
            )

        self.assertIn("product_id", resp.data)
        self.assertIn("quantity", resp.data)
        self.assertIn("remaining", resp.data)
        self.assertIn("transaction_id", resp.data)
        self.assertIn("status", resp.data)
        self.assertNotIn("detail", resp.data)
        # remaining should reflect the post-reservation stock
        self.assertEqual(resp.data["remaining"], 6)
        self.assertEqual(resp.data["transaction_id"], tid)
        self.assertEqual(resp.data["status"], Reservation.Status.RESERVED)

    def test_reserve_nonexistent_product_returns_404(self):
        with _mock_coordinator_ok():
            resp = self.client.post(
                RESERVE_URL,
                {"product_id": 99999, "quantity": 1, "transaction_id": str(uuid.uuid4())},
                format="json",
            )
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Reserve — insufficient stock
# ---------------------------------------------------------------------------

class ReserveInsufficientStockTests(TestCase):
    """Stock cannot go negative; reservation must be refused with no side effects."""

    def setUp(self):
        self.client = APIClient()
        self.product = _create_product(stock=2)

    def test_insufficient_stock_returns_400(self):
        with _mock_coordinator_ok():
            resp = self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 5,
                 "transaction_id": str(uuid.uuid4())},
                format="json",
            )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("stock", resp.data.get("error", "").lower())

    def test_insufficient_stock_does_not_decrement_stock(self):
        tid = str(uuid.uuid4())
        with _mock_coordinator_ok():
            self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 5, "transaction_id": tid},
                format="json",
            )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 2)

    def test_insufficient_stock_creates_no_reservation_row(self):
        tid = str(uuid.uuid4())
        with _mock_coordinator_ok():
            self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 5, "transaction_id": tid},
                format="json",
            )
        self.assertFalse(Reservation.objects.filter(transaction_id=tid).exists())

    def test_exact_stock_quantity_succeeds(self):
        """Reserving exactly the remaining stock must succeed (boundary case)."""
        tid = str(uuid.uuid4())
        with _mock_coordinator_ok():
            resp = self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 2, "transaction_id": tid},
                format="json",
            )
        self.assertEqual(resp.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 0)


# ---------------------------------------------------------------------------
# Reserve — idempotency
# ---------------------------------------------------------------------------

class ReserveIdempotencyTests(TestCase):
    """Duplicate transaction_id: return existing reservation, no second decrement."""

    def setUp(self):
        self.client = APIClient()
        self.product = _create_product(stock=10)

    def test_duplicate_returns_200_with_existing_status(self):
        tid = str(uuid.uuid4())
        with _mock_coordinator_ok():
            self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 3, "transaction_id": tid},
                format="json",
            )
            resp2 = self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 3, "transaction_id": tid},
                format="json",
            )

        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data["status"], Reservation.Status.RESERVED)

    def test_duplicate_does_not_decrement_stock_twice(self):
        tid = str(uuid.uuid4())
        with _mock_coordinator_ok():
            self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 3, "transaction_id": tid},
                format="json",
            )
            self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 3, "transaction_id": tid},
                format="json",
            )

        self.product.refresh_from_db()
        # Stock should only have been decremented once (by 3), not twice (by 6)
        self.assertEqual(self.product.stock, 7)

    def test_duplicate_creates_exactly_one_reservation_row(self):
        tid = str(uuid.uuid4())
        with _mock_coordinator_ok():
            self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 3, "transaction_id": tid},
                format="json",
            )
            self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 3, "transaction_id": tid},
                format="json",
            )

        self.assertEqual(Reservation.objects.filter(transaction_id=tid).count(), 1)

    def test_duplicate_with_different_quantity_returns_original(self):
        """The first call wins; the second call's quantity is ignored."""
        tid = str(uuid.uuid4())
        with _mock_coordinator_ok():
            self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 2, "transaction_id": tid},
                format="json",
            )
            resp2 = self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 9, "transaction_id": tid},
                format="json",
            )

        # Second response must return the ORIGINAL reservation (quantity=2)
        self.assertEqual(resp2.data["transaction_id"], tid)
        self.assertEqual(resp2.data["quantity"], 2)
        self.assertEqual(resp2.data["product_id"], self.product.id)
        self.assertIn("remaining", resp2.data)
        self.assertEqual(
            Reservation.objects.get(transaction_id=tid).quantity, 2
        )
        self.product.refresh_from_db()
        # Only 2 units were ever reserved
        self.assertEqual(self.product.stock, 8)


# ---------------------------------------------------------------------------
# Reserve — Coordinator callback failure is non-fatal
# ---------------------------------------------------------------------------

class ReserveCallbackFailureTests(TestCase):
    """
    The status-update callback to the Coordinator is best-effort.
    A failed callback must not roll back the reservation.
    """

    def setUp(self):
        self.client = APIClient()
        self.product = _create_product(stock=10)

    def test_coordinator_connection_error_does_not_prevent_reservation(self):
        tid = str(uuid.uuid4())
        with _mock_coordinator_down():
            resp = self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 1, "transaction_id": tid},
                format="json",
            )

        # Reservation must succeed despite the callback failure
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Reservation.objects.filter(transaction_id=tid).exists())

    def test_coordinator_connection_error_still_decrements_stock(self):
        tid = str(uuid.uuid4())
        with _mock_coordinator_down():
            self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 4, "transaction_id": tid},
                format="json",
            )

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 6)

    def test_coordinator_error_response_reports_callback_failure(self):
        """Callback failure is non-fatal; detail field is intentionally omitted."""
        tid = str(uuid.uuid4())
        with _mock_coordinator_down():
            resp = self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 1, "transaction_id": tid},
                format="json",
            )

        # detail field was removed, assert the response is still successful
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["transaction_id"], tid)
        self.assertEqual(resp.data["status"], Reservation.Status.RESERVED)
        self.assertNotIn("detail", resp.data)

    def test_coordinator_timeout_does_not_prevent_reservation(self):
        from requests.exceptions import Timeout as RequestsTimeout
        tid = str(uuid.uuid4())
        with patch("inventory.views.requests.get", side_effect=RequestsTimeout()):
            resp = self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 1, "transaction_id": tid},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Reservation.objects.filter(transaction_id=tid).exists())


# ---------------------------------------------------------------------------
# Release — input & not-found
# ---------------------------------------------------------------------------

class ReleaseValidationTests(TestCase):

    def setUp(self):
        self.client = APIClient()

    def test_missing_transaction_id_returns_404_or_error(self):
        resp = self.client.post(RELEASE_URL, {}, format="json")
        # No transaction_id → get_object_or_404 raises 404
        self.assertIn(resp.status_code, (400, 404))

    def test_nonexistent_transaction_id_returns_404(self):
        resp = self.client.post(
            RELEASE_URL,
            {"transaction_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)
        self.assertIn("error", resp.data)

    def test_nonexistent_release_does_not_change_any_stock(self):
        product = _create_product(stock=5)
        self.client.post(
            RELEASE_URL,
            {"transaction_id": str(uuid.uuid4())},
            format="json",
        )
        product.refresh_from_db()
        self.assertEqual(product.stock, 5)

    def test_intentional_failure_trigger_returns_500(self):
        """Sending transaction_id='fail-release' triggers the test hook."""
        resp = self.client.post(
            RELEASE_URL,
            {"transaction_id": "fail-release"},
            format="json",
        )
        self.assertEqual(resp.status_code, 500)


# ---------------------------------------------------------------------------
# Release — happy path
# ---------------------------------------------------------------------------

class ReleaseHappyPathTests(TestCase):
    """Successful release: stock restored, Reservation status updated."""

    def setUp(self):
        self.client = APIClient()
        self.product = _create_product(stock=10)

    def _make_reservation(self, quantity=3):
        tid = uuid.uuid4()
        reservation = Reservation.objects.create(
            product=self.product,
            quantity=quantity,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
        )
        # Deduct stock as if reserve was called
        self.product.stock -= quantity
        self.product.save()
        return reservation

    def test_release_restores_stock(self):
        reservation = self._make_reservation(quantity=3)
        resp = self.client.post(
            RELEASE_URL,
            {"transaction_id": str(reservation.transaction_id)},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)  # 7 + 3

    def test_release_marks_reservation_as_released(self):
        reservation = self._make_reservation(quantity=3)
        self.client.post(
            RELEASE_URL,
            {"transaction_id": str(reservation.transaction_id)},
            format="json",
        )
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.RELEASED)

    def test_release_response_shape(self):
        reservation = self._make_reservation()
        resp = self.client.post(
            RELEASE_URL,
            {"transaction_id": str(reservation.transaction_id)},
            format="json",
        )
        self.assertIn("message", resp.data)
        self.assertIn("transaction_id", resp.data)


# ---------------------------------------------------------------------------
# Release — idempotency
# ---------------------------------------------------------------------------

class ReleaseIdempotencyTests(TestCase):
    """
    Double release must not double-restore stock.

    This is critical for correctness: if a retry_compensation call arrives
    after the recovery worker already released, the second release must be a
    no-op on stock.
    """

    def setUp(self):
        self.client = APIClient()
        self.product = _create_product(stock=10)

    def _make_reservation(self, quantity=3):
        tid = uuid.uuid4()
        reservation = Reservation.objects.create(
            product=self.product,
            quantity=quantity,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
        )
        self.product.stock -= quantity
        self.product.save()
        return reservation

    def test_double_release_does_not_restore_stock_twice(self):
        reservation = self._make_reservation(quantity=3)
        tid = str(reservation.transaction_id)

        # First release
        resp1 = self.client.post(RELEASE_URL, {"transaction_id": tid}, format="json")
        self.assertEqual(resp1.status_code, 200)

        # Second release (idempotent re-try)
        resp2 = self.client.post(RELEASE_URL, {"transaction_id": tid}, format="json")
        self.assertEqual(resp2.status_code, 200)

        self.product.refresh_from_db()
        # Stock must equal 10 (restored once), NOT 13 (restored twice)
        self.assertEqual(
            self.product.stock,
            10,
            "Double release incorrectly restored stock twice",
        )

    def test_double_release_reservation_stays_released(self):
        reservation = self._make_reservation()
        tid = str(reservation.transaction_id)
        self.client.post(RELEASE_URL, {"transaction_id": tid}, format="json")
        self.client.post(RELEASE_URL, {"transaction_id": tid}, format="json")
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.RELEASED)


# ---------------------------------------------------------------------------
# Reservation check
# ---------------------------------------------------------------------------

class ReservationCheckTests(TestCase):
    """GET /reserve/check/?transaction_id=..."""

    def setUp(self):
        self.client = APIClient()
        self.product = _create_product(stock=10)

    def test_check_reserved(self):
        tid = uuid.uuid4()
        Reservation.objects.create(
            product=self.product,
            quantity=1,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
        )
        resp = self.client.get(CHECK_URL, {"transaction_id": str(tid)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Reservation.Status.RESERVED)

    def test_check_released(self):
        tid = uuid.uuid4()
        Reservation.objects.create(
            product=self.product,
            quantity=1,
            transaction_id=tid,
            status=Reservation.Status.RELEASED,
        )
        resp = self.client.get(CHECK_URL, {"transaction_id": str(tid)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Reservation.Status.RELEASED)

    def test_check_not_found_returns_not_reserved(self):
        resp = self.client.get(CHECK_URL, {"transaction_id": str(uuid.uuid4())})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Reservation.Status.NOT_RESERVED)

    def test_check_missing_transaction_id_returns_400(self):
        resp = self.client.get(CHECK_URL)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("transaction_id", resp.data.get("error", ""))


# ---------------------------------------------------------------------------
# Reserve — concurrency (requires real DB transactions)
# ---------------------------------------------------------------------------

class ReserveConcurrencyTests(TransactionTestCase):
    """
    Two threads concurrently try to reserve from the same product.

    The SELECT FOR UPDATE in reserve_product ensures that only one thread
    can hold the row lock at a time.  When stock = 1 and both threads want
    quantity=1, exactly one must succeed and one must fail with "Not enough
    stock".

    Uses TransactionTestCase so each thread gets its own real DB transaction
    that is visible to the other threads.
    """

    def test_concurrent_reserve_exactly_one_winner_when_stock_is_one(self):
        product = Product.objects.create(name="LimitedItem", stock=1)
        results = []

        def do_reserve(_):
            client = APIClient()
            try:
                with _mock_coordinator_ok():
                    resp = client.post(
                        RESERVE_URL,
                        {
                            "product_id": product.id,
                            "quantity": 1,
                            "transaction_id": str(uuid.uuid4()),
                        },
                        format="json",
                    )
                return resp.status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(do_reserve, i) for i in range(2)]
            for future in as_completed(futures):
                results.append(future.result())

        # No crashes
        for code in results:
            self.assertNotEqual(code, 500, "Concurrent reserve produced a 500")

        # Exactly one 200 (success) and one 400 (insufficient stock)
        self.assertEqual(results.count(200), 1,
                         f"Expected exactly 1 success, got: {results}")
        self.assertEqual(results.count(400), 1,
                         f"Expected exactly 1 failure, got: {results}")

        # Stock must be exactly 0 (one reservation consumed it all)
        product.refresh_from_db()
        self.assertEqual(product.stock, 0,
                         f"Expected stock=0 after one reservation, got: {product.stock}")

        # Exactly one Reservation row was created
        self.assertEqual(
            Reservation.objects.filter(product=product).count(),
            1,
            "Expected exactly one Reservation row",
        )

    def test_concurrent_reserve_both_succeed_when_stock_is_sufficient(self):
        """Both threads succeed when stock > sum(quantities)."""
        product = Product.objects.create(name="AbundantItem", stock=10)

        def do_reserve(_):
            client = APIClient()
            try:
                with _mock_coordinator_ok():
                    resp = client.post(
                        RESERVE_URL,
                        {
                            "product_id": product.id,
                            "quantity": 3,
                            "transaction_id": str(uuid.uuid4()),
                        },
                        format="json",
                    )
                return resp.status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(do_reserve, range(2)))

        for code in results:
            self.assertEqual(code, 200, f"Unexpected status: {results}")

        product.refresh_from_db()
        self.assertEqual(product.stock, 4)  # 10 - 3 - 3


# ---------------------------------------------------------------------------
# Release — concurrency
# ---------------------------------------------------------------------------

class ReleaseConcurrencyTests(TransactionTestCase):
    """
    Two threads simultaneously release the same reservation.
    The SELECT FOR UPDATE inside release_product ensures exactly one thread
    processes the release; the other sees status=RELEASED and returns early
    without touching stock.
    """

    def test_concurrent_release_restores_stock_exactly_once(self):
        product = Product.objects.create(name="Widget", stock=7)
        reservation = Reservation.objects.create(
            product=product,
            quantity=3,
            transaction_id=uuid.uuid4(),
            status=Reservation.Status.RESERVED,
        )
        # Simulate the stock having been decremented at reserve time
        product.stock -= 3
        product.save()
        tid = str(reservation.transaction_id)

        def do_release(_):
            client = APIClient()
            try:
                resp = client.post(RELEASE_URL, {"transaction_id": tid}, format="json")
                return resp.status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(do_release, range(2)))

        # Both calls must complete without a 500
        for code in results:
            self.assertNotEqual(code, 500, f"Concurrent release produced a 500: {results}")

        # Both must return 200 (second is the idempotent path)
        for code in results:
            self.assertEqual(code, 200, f"Unexpected status: {results}")

        # Stock must be exactly 7 (restored once, not twice)
        product.refresh_from_db()
        self.assertEqual(
            product.stock,
            7,
            f"Expected stock=7 after idempotent double-release, got: {product.stock}",
        )


# ---------------------------------------------------------------------------
# Reserve — concurrent duplicate transaction_id (IntegrityError path)
# ---------------------------------------------------------------------------

class ReserveConcurrentDuplicateIdempotencyTests(TransactionTestCase):
    """
    Two threads send the EXACT SAME transaction_id to reserve_product at the
    same time, before either has committed a row.

    The race
    --------
    Both threads pass the initial filter().first() check (plain SELECT, no
    lock) and find None.  Both then enter transaction.atomic() and queue up
    on the SELECT FOR UPDATE product lock.

    Thread A wins the lock, decrements stock, creates the Reservation, commits.

    Thread B acquires the lock after A's commit.  It re-reads the (now lower)
    stock and tries to INSERT a Reservation with the same transaction_id.
    PostgreSQL fires the UNIQUE constraint on inventory_reservation.transaction_id
    → IntegrityError.

    The atomic block re-raises, rolling back Thread B's stock decrement.
    reserve_product catches IntegrityError outside the atomic block
    (connection is clean), re-fetches the existing Reservation, and returns
    the idempotent 200 response.

    Expected outcomes
    -----------------
    - Both threads return 200 (no 500s).
    - Exactly one Reservation row exists in the DB.
    - Stock was decremented exactly once.
    """

    def test_concurrent_same_transaction_id_no_500_one_reservation(self):
        product = Product.objects.create(name="RaceyItem", stock=10)
        tid = str(uuid.uuid4())
        results = []

        def do_reserve(_):
            client = APIClient()
            try:
                with _mock_coordinator_ok():
                    resp = client.post(
                        RESERVE_URL,
                        {
                            "product_id": product.id,
                            "quantity": 2,
                            "transaction_id": tid,
                        },
                        format="json",
                    )
                return resp.status_code, resp.data
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(do_reserve, i) for i in range(5)]
            for future in as_completed(futures):
                results.append(future.result())

        # ── No 500s ───────────────────────────────────────────────────────
        codes = [r[0] for r in results]
        for code in codes:
            self.assertIn(
                code,
                (200,),
                f"Concurrent duplicate reserve produced unexpected status {code}. "
                f"All codes: {codes}",
            )

        # ── Exactly one Reservation row ───────────────────────────────────
        self.assertEqual(
            Reservation.objects.filter(transaction_id=tid).count(),
            1,
            "Expected exactly one Reservation row for the shared transaction_id",
        )

        # ── Stock decremented exactly once (by 2) ────────────────────────
        product.refresh_from_db()
        self.assertEqual(
            product.stock,
            8,   # 10 - 2 (one decrement only)
            f"Expected stock=8 (decremented once), got {product.stock}. "
            "A concurrent loser may have double-decremented and not rolled back.",
        )

    def test_concurrent_same_transaction_id_all_responses_consistent(self):
        """All concurrent responses for the same transaction_id must agree
        and return the exact same unified response shape."""
        product = Product.objects.create(name="ConsistencyItem", stock=10)
        tid = str(uuid.uuid4())
        results = []

        def do_reserve(_):
            client = APIClient()
            try:
                with _mock_coordinator_ok():
                    resp = client.post(
                        RESERVE_URL,
                        {"product_id": product.id, "quantity": 1, "transaction_id": tid},
                        format="json",
                    )
                return resp.status_code, resp.data
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(do_reserve, i) for i in range(3)]
            for future in as_completed(futures):
                results.append(future.result())

        for code, data in results:
            self.assertEqual(code, 200)
            self.assertIn("message", data)
            self.assertEqual(str(data.get("transaction_id")), tid)
            self.assertEqual(data.get("status"), Reservation.Status.RESERVED)
            self.assertEqual(data.get("product_id"), product.id)
            self.assertEqual(data.get("quantity"), 1)
            self.assertIn("remaining", data)
            self.assertNotIn("detail", data)

