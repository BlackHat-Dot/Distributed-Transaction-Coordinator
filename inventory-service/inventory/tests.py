import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch, MagicMock

from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from .models import Product, Reservation


RESERVE_URL = "/products/reserve/"
RELEASE_URL = "/products/release/"
CHECK_URL = "/reserve/check/"
PRODUCTS_URL = "/products/"
HEALTH_URL = "/health/"


def _mock_coordinator_ok():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    return patch("inventory.views.requests.get", return_value=mock_resp)


def _mock_coordinator_down():
    from requests.exceptions import ConnectionError as RequestsConnectionError
    return patch("inventory.views.requests.get", side_effect=RequestsConnectionError())


def _create_product(name="Widget", stock=10):
    return Product.objects.create(name=name, stock=stock)


class HealthCheckTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_health_returns_200(self):
        resp = self.client.get(HEALTH_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "healthy")


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


class ReserveValidationTests(TestCase):
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
        self.assertNotEqual(resp.status_code, 500)


class ReserveHappyPathTests(TestCase):
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


class ReserveInsufficientStockTests(TestCase):
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


class ReserveIdempotencyTests(TestCase):
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

        self.assertEqual(resp2.data["transaction_id"], tid)
        self.assertEqual(resp2.data["quantity"], 2)
        self.assertEqual(resp2.data["product_id"], self.product.id)
        self.assertIn("remaining", resp2.data)
        self.assertEqual(
            Reservation.objects.get(transaction_id=tid).quantity, 2
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 8)


class ReserveCallbackFailureTests(TestCase):
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
        tid = str(uuid.uuid4())
        with _mock_coordinator_down():
            resp = self.client.post(
                RESERVE_URL,
                {"product_id": self.product.id, "quantity": 1, "transaction_id": tid},
                format="json",
            )

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


class ReleaseValidationTests(TestCase):

    def setUp(self):
        self.client = APIClient()

    def test_missing_transaction_id_returns_404_or_error(self):
        resp = self.client.post(RELEASE_URL, {}, format="json")
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
        resp = self.client.post(
            RELEASE_URL,
            {"transaction_id": "fail-release"},
            format="json",
        )
        self.assertEqual(resp.status_code, 500)


class ReleaseHappyPathTests(TestCase):
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


class ReleaseIdempotencyTests(TestCase):
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

        resp1 = self.client.post(RELEASE_URL, {"transaction_id": tid}, format="json")
        self.assertEqual(resp1.status_code, 200)

        resp2 = self.client.post(RELEASE_URL, {"transaction_id": tid}, format="json")
        self.assertEqual(resp2.status_code, 200)

        self.product.refresh_from_db()
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



class ReservationCheckTests(TestCase):
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


class ReserveConcurrencyTests(TransactionTestCase):
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

        for code in results:
            self.assertNotEqual(code, 500, "Concurrent reserve produced a 500")

        self.assertEqual(results.count(200), 1,
                         f"Expected exactly 1 success, got: {results}")
        self.assertEqual(results.count(400), 1,
                         f"Expected exactly 1 failure, got: {results}")

        product.refresh_from_db()
        self.assertEqual(product.stock, 0,
                         f"Expected stock=0 after one reservation, got: {product.stock}")

        self.assertEqual(
            Reservation.objects.filter(product=product).count(),
            1,
            "Expected exactly one Reservation row",
        )

    def test_concurrent_reserve_both_succeed_when_stock_is_sufficient(self):
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



class ReleaseConcurrencyTests(TransactionTestCase):
    def test_concurrent_release_restores_stock_exactly_once(self):
        product = Product.objects.create(name="Widget", stock=7)
        reservation = Reservation.objects.create(
            product=product,
            quantity=3,
            transaction_id=uuid.uuid4(),
            status=Reservation.Status.RESERVED,
        )

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

        for code in results:
            self.assertNotEqual(code, 500, f"Concurrent release produced a 500: {results}")

        for code in results:
            self.assertEqual(code, 200, f"Unexpected status: {results}")

        product.refresh_from_db()
        self.assertEqual(
            product.stock,
            7,
            f"Expected stock=7 after idempotent double-release, got: {product.stock}",
        )


class ReserveConcurrentDuplicateIdempotencyTests(TransactionTestCase):

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

        codes = [r[0] for r in results]
        for code in codes:
            self.assertIn(
                code,
                (200,),
                f"Concurrent duplicate reserve produced unexpected status {code}. "
                f"All codes: {codes}",
            )

        self.assertEqual(
            Reservation.objects.filter(transaction_id=tid).count(),
            1,
            "Expected exactly one Reservation row for the shared transaction_id",
        )


        product.refresh_from_db()
        self.assertEqual(
            product.stock,
            8,
            f"Expected stock=8 (decremented once), got {product.stock}. "
            "A concurrent loser may have double-decremented and not rolled back.",
        )

    def test_concurrent_same_transaction_id_all_responses_consistent(self):
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

