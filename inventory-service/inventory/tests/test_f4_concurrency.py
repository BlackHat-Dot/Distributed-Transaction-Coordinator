import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from inventory.models import Event, Seat, Reservation
from inventory.services import reclaim_expired_holds


class TestF4RealConcurrency(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            raise unittest.SkipTest("F4: Concurrency tests require PostgreSQL for row locks and transactions")
        self.event = Event.objects.create(name="F4 Concurrency Tour")

    def test_20_threads_reserving_same_seat_yields_single_winner(self):
        """
        F4: 20 threads simultaneously reserving the same seat.
        Exactly one thread gets 200, all other 19 get 409.
        """
        seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="1",
            price=Decimal("100.00"),
            status=Seat.Status.AVAILABLE,
        )

        concurrency = 20
        barrier = threading.Barrier(concurrency)
        results = []

        def worker(worker_id):
            client = APIClient()
            tid = str(uuid.uuid4())
            try:
                barrier.wait(timeout=5)
                resp = client.post(
                    "/seats/reserve/",
                    {
                        "seat_ids": [seat.id],
                        "transaction_id": tid,
                        "user_id": f"user_{worker_id}",
                    },
                    format="json",
                )
                return resp.status_code, resp.data
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(worker, i) for i in range(concurrency)]
            for f in as_completed(futures):
                results.append(f.result())

        status_codes = [r[0] for r in results]
        self.assertEqual(
            status_codes.count(200),
            1,
            f"Expected exactly 1 winner with status 200, got: {status_codes}",
        )
        self.assertEqual(
            status_codes.count(409),
            concurrency - 1,
            f"Expected {concurrency - 1} conflicts with status 409, got: {status_codes}",
        )

        seat.refresh_from_db()
        self.assertEqual(seat.status, Seat.Status.HELD)
        self.assertEqual(
            Reservation.objects.filter(seat=seat, status=Reservation.Status.RESERVED).count(),
            1,
        )

    def test_reserve_retry_racing_reclaim_expired_holds(self):
        """
        F4: Reserve retry (same transaction_id) racing reclaim_expired_holds.
        If the retry succeeds (returns 200), the seat must never end up AVAILABLE.
        """
        tid = uuid.uuid4()
        now = timezone.now()
        seat = Seat.objects.create(
            event=self.event,
            row="B",
            seat_number="1",
            price=Decimal("100.00"),
            status=Seat.Status.HELD,
            held_until=now - timedelta(seconds=2),
            held_by_transaction_id=tid,
            user_id="usr_retry_race",
            user_email="retry_race@example.com",
        )
        Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
            user_id="usr_retry_race",
            user_email="retry_race@example.com",
        )

        barrier = threading.Barrier(2)
        outcomes = {}

        def reserve_worker():
            client = APIClient()
            try:
                barrier.wait(timeout=5)
                resp = client.post(
                    "/seats/reserve/",
                    {
                        "seat_ids": [seat.id],
                        "transaction_id": str(tid),
                        "user_id": "usr_retry_race",
                        "user_email": "retry_race@example.com",
                    },
                    format="json",
                )
                outcomes["reserve"] = resp.status_code
            finally:
                connection.close()

        def reclaim_worker():
            try:
                barrier.wait(timeout=5)
                reclaimed, released = reclaim_expired_holds()
                outcomes["reclaim"] = (reclaimed, released)
            finally:
                connection.close()

        t1 = threading.Thread(target=reserve_worker)
        t2 = threading.Thread(target=reclaim_worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        seat.refresh_from_db()
        reserve_code = outcomes.get("reserve")
        # Invariant: If retry returned 200, seat must NEVER end up AVAILABLE!
        if reserve_code == 200:
            self.assertEqual(
                seat.status,
                Seat.Status.HELD,
                f"Seat was reclaimed into AVAILABLE even though reserve retry returned 200! outcomes={outcomes}",
            )
            self.assertTrue(seat.held_until > timezone.now())

    def test_confirm_racing_reclaim_on_expiring_hold_yields_consistent_state(self):
        """
        F4: confirm_seat racing reclaim_expired_holds on an expiring hold.
        Final state must be strictly consistent:
        Either (seat BOOKED and res BOOKED) or (seat AVAILABLE and res RELEASED).
        Never an orphaned or mismatched state.
        """
        tid = uuid.uuid4()
        now = timezone.now()
        seat = Seat.objects.create(
            event=self.event,
            row="C",
            seat_number="1",
            price=Decimal("100.00"),
            status=Seat.Status.HELD,
            held_until=now - timedelta(milliseconds=50),
            held_by_transaction_id=tid,
            user_id="usr_confirm_race",
            user_email="confirm_race@example.com",
        )
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
            user_id="usr_confirm_race",
            user_email="confirm_race@example.com",
        )

        internal_key = getattr(settings, "INTERNAL_SERVICE_KEY", "dtc-internal-coordinator-secret-key-2026")
        barrier = threading.Barrier(2)
        outcomes = {}

        def confirm_worker():
            client = APIClient()
            try:
                barrier.wait(timeout=5)
                resp = client.post(
                    "/seats/confirm/",
                    {"seat_ids": [seat.id], "transaction_id": str(tid)},
                    format="json",
                    HTTP_X_INTERNAL_SERVICE_KEY=internal_key,
                )
                outcomes["confirm"] = resp.status_code
            finally:
                connection.close()

        def reclaim_worker():
            try:
                barrier.wait(timeout=5)
                reclaimed, released = reclaim_expired_holds()
                outcomes["reclaim"] = (reclaimed, released)
            finally:
                connection.close()

        t1 = threading.Thread(target=confirm_worker)
        t2 = threading.Thread(target=reclaim_worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        seat.refresh_from_db()
        res.refresh_from_db()

        # Both entities must be completely consistent:
        valid_booked = (seat.status == Seat.Status.BOOKED and res.status == Reservation.Status.BOOKED)
        valid_released = (seat.status == Seat.Status.AVAILABLE and res.status == Reservation.Status.RELEASED)

        self.assertTrue(
            valid_booked or valid_released,
            f"Inconsistent state detected! seat.status={seat.status}, res.status={res.status}, outcomes={outcomes}",
        )
