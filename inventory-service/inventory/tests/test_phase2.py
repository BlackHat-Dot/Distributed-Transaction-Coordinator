import threading
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TransactionTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from inventory.models import Event, Seat, Reservation
from inventory.views import reclaim_expired_holds, reserve_seat, release_seat


class Phase2PresenceAndPipelineTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = Event.objects.create(name="Festival", starts_at=timezone.now() + timedelta(days=3))
        self.internal_key = "dtc-internal-coordinator-secret-key-2026"

    def test_presence_reclaim_skips_holds_within_grace_period(self):
        """
        Tests that presence-based reclaim does NOT reclaim a seat if the hold
        was created less than HOLD_PRESENCE_GRACE_SECONDS (120s) ago,
        even if redis session does not exist.
        """
        now = timezone.now()
        tid = uuid.uuid4()
        # Created 30 seconds ago (hold duration = 10m -> held_until = now + 9m30s)
        held_until = now + timedelta(minutes=10) - timedelta(seconds=30)
        seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="1",
            price=100.00,
            status=Seat.Status.HELD,
            held_until=held_until,
            held_by_transaction_id=tid,
            user_email="young_hold@example.com",
        )
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
            user_email="young_hold@example.com",
        )

        mock_redis = MagicMock()
        mock_redis.pipeline.return_value.execute.return_value = [False]

        with patch("inventory.services.get_redis_client", return_value=mock_redis):
            reclaimed, released = reclaim_expired_holds()

        seat.refresh_from_db()
        res.refresh_from_db()

        # Hold is within 120s grace period -> MUST NOT be reclaimed
        self.assertEqual(reclaimed, 0)
        self.assertEqual(seat.status, Seat.Status.HELD)
        self.assertEqual(res.status, Reservation.Status.RESERVED)

    def test_presence_reclaim_frees_holds_older_than_grace_period(self):
        """
        Tests that presence-based reclaim DOES reclaim a seat if the hold
        was created >= 120s ago and redis session does not exist.
        """
        now = timezone.now()
        tid = uuid.uuid4()
        # Created 150 seconds ago (hold duration = 10m -> held_until = now + 7m30s)
        held_until = now + timedelta(minutes=10) - timedelta(seconds=150)
        seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="2",
            price=100.00,
            status=Seat.Status.HELD,
            held_until=held_until,
            held_by_transaction_id=tid,
            user_email="old_hold@example.com",
        )
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
            user_email="old_hold@example.com",
        )

        mock_redis = MagicMock()
        # Session does not exist in redis
        mock_redis.pipeline.return_value.execute.return_value = [False]

        with patch("inventory.services.get_redis_client", return_value=mock_redis):
            reclaimed, released = reclaim_expired_holds()

        seat.refresh_from_db()
        res.refresh_from_db()

        # Hold is older than 120s grace period -> MUST be reclaimed
        self.assertEqual(reclaimed, 1)
        self.assertEqual(released, 1)
        self.assertEqual(seat.status, Seat.Status.AVAILABLE)
        self.assertEqual(res.status, Reservation.Status.RELEASED)

    def test_presence_reclaim_fails_safe_on_redis_error(self):
        """
        Tests that when Redis raises an exception during pipelining,
        the presence sweep fails safe and does not drop active holds.
        """
        now = timezone.now()
        tid = uuid.uuid4()
        held_until = now + timedelta(minutes=10) - timedelta(seconds=200)
        seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="3",
            price=100.00,
            status=Seat.Status.HELD,
            held_until=held_until,
            held_by_transaction_id=tid,
            user_email="redis_down@example.com",
        )
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
            user_email="redis_down@example.com",
        )

        mock_redis = MagicMock()
        mock_redis.pipeline.side_effect = Exception("Redis connection refused")

        with patch("inventory.services.get_redis_client", return_value=mock_redis):
            reclaimed, released = reclaim_expired_holds()

        seat.refresh_from_db()
        res.refresh_from_db()

        # Fails safe -> hold is NOT dropped
        self.assertEqual(reclaimed, 0)
        self.assertEqual(seat.status, Seat.Status.HELD)


class Phase2TwoThreadConcurrencyDeadlockTest(TransactionTestCase):
    """
    Runs real concurrent multi-threaded requests on PostgreSQL to verify
    the lock hierarchy (Seats first, Reservations second) prevents deadlocks.
    """
    databases = {"default"}

    def setUp(self):
        self.event = Event.objects.create(name="Live Match", starts_at=timezone.now() + timedelta(days=2))
        self.seats = [
            Seat.objects.create(event=self.event, row="L", seat_number=str(i), price=150.00)
            for i in range(1, 4)
        ]
        self.tid = str(uuid.uuid4())
        self.internal_key = "dtc-internal-coordinator-secret-key-2026"

    def test_concurrent_reserve_and_release_no_deadlock(self):
        factory = APIRequestFactory()
        seat_ids = [s.id for s in self.seats]

        errors = []
        status_codes = []

        def run_reserve():
            try:
                # Close old connection to force separate DB connection per thread
                connection.close()
                req = factory.post(
                    "/seats/reserve/",
                    {"seat_ids": seat_ids, "transaction_id": self.tid, "user_id": "thread_user"},
                    format="json",
                    HTTP_X_INTERNAL_SERVICE_KEY=self.internal_key,
                )
                resp = reserve_seat(req)
                status_codes.append(("reserve", resp.status_code))
            except Exception as e:
                errors.append(("reserve", e))
            finally:
                connection.close()

        def run_release():
            try:
                connection.close()
                req = factory.post(
                    "/seats/release/",
                    {"seat_ids": seat_ids, "transaction_id": self.tid},
                    format="json",
                    HTTP_X_INTERNAL_SERVICE_KEY=self.internal_key,
                )
                resp = release_seat(req)
                status_codes.append(("release", resp.status_code))
            except Exception as e:
                errors.append(("release", e))
            finally:
                connection.close()

        # Initially reserve the seats so release has something to operate on
        initial_req = factory.post(
            "/seats/reserve/",
            {"seat_ids": seat_ids, "transaction_id": self.tid, "user_id": "thread_user"},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY=self.internal_key,
        )
        initial_resp = reserve_seat(initial_req)
        self.assertEqual(initial_resp.status_code, 200)

        # Launch concurrent threads: Thread 1 retrying reserve, Thread 2 executing release
        t1 = threading.Thread(target=run_reserve)
        t2 = threading.Thread(target=run_release)

        t1.start()
        t2.start()

        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        # Assert neither thread timed out or raised a DB deadlock error
        self.assertFalse(t1.is_alive(), "Thread 1 deadlocked or hung!")
        self.assertFalse(t2.is_alive(), "Thread 2 deadlocked or hung!")
        self.assertEqual(errors, [], f"Unexpected exceptions in threads: {errors}")

        # Final state check: Seat status must be consistent with Reservation status
        connection.close()
        for seat in Seat.objects.filter(id__in=seat_ids):
            res = Reservation.objects.filter(seat=seat, transaction_id=uuid.UUID(self.tid)).first()
            if seat.status == Seat.Status.AVAILABLE:
                self.assertIn(res.status, [Reservation.Status.RELEASED, Reservation.Status.NOT_RESERVED])
            elif seat.status == Seat.Status.HELD:
                self.assertEqual(res.status, Reservation.Status.RESERVED)
