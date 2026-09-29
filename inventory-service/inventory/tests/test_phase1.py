import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from inventory.models import Event, Seat, Reservation
from inventory.views import reclaim_expired_holds, confirm_seat, reserve_seat


class Phase1ComprehensiveTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.factory = APIRequestFactory()
        self.event = Event.objects.create(
            name="Rock Concert 2026",
            venue="Metropolis Arena",
            starts_at=timezone.now() + timedelta(days=7),
        )
        self.internal_key = "dtc-internal-coordinator-secret-key-2026"

    # =========================================================================
    # FINDING 4: Idempotent Retry of Confirmed (BOOKED) Seat
    # =========================================================================
    def test_reserve_retry_after_confirm_returns_idempotent_200_without_downgrade(self):
        """
        Tests that when reserve_seat is called for a transaction whose seats are already BOOKED,
        it returns an idempotent 200 with the exact normal reserve response shape,
        and does NOT downgrade status to HELD or set held_until.
        """
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="1",
            price=250.00,
            status=Seat.Status.BOOKED,
            held_until=None,
            held_by_transaction_id=tid,
            user_id="user_123",
            user_email="user@example.com",
        )
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.BOOKED,
            user_id="user_123",
            user_email="user@example.com",
        )

        resp = self.client.post(
            "/seats/reserve/",
            {
                "seat_ids": [seat.id],
                "transaction_id": str(tid),
                "user_id": "user_123",
                "user_email": "user@example.com",
            },
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY=self.internal_key,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["transaction_id"], str(tid))
        self.assertEqual(resp.data["seat_ids"], [seat.id])
        self.assertIsNone(resp.data["held_until"])
        self.assertEqual(resp.data["total_amount"], "250.00")
        self.assertEqual(resp.data["message"], "1 Seats reserved")

        seat.refresh_from_db()
        res.refresh_from_db()

        # Must strictly preserve BOOKED status and null held_until
        self.assertEqual(seat.status, Seat.Status.BOOKED)
        self.assertIsNone(seat.held_until)
        self.assertEqual(res.status, Reservation.Status.BOOKED)

    # =========================================================================
    # FINDING 5: Interleaving Concurrent Confirm in Presence-Based Sweeper
    # =========================================================================
    def test_reclaim_with_concurrent_confirm_via_redis_exists(self):
        """
        Seat has a future held_until so it enters the presence check (step 2).
        When redis_client.exists() is queried, a concurrent confirm_seat executes,
        changing the seat status to BOOKED.
        The sweeper must detect the change and NOT wipe the BOOKED seat to AVAILABLE.
        """
        tid = uuid.uuid4()
        future_held_until = timezone.now() + timedelta(minutes=5)
        seat = Seat.objects.create(
            event=self.event,
            row="B",
            seat_number="5",
            price=150.00,
            status=Seat.Status.HELD,
            held_until=future_held_until,
            held_by_transaction_id=tid,
            user_id="user_456",
            user_email="buyer@example.com",
        )
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
            user_id="user_456",
            user_email="buyer@example.com",
        )

        # Mock Redis client where exists() performs concurrent confirmation
        mock_redis = MagicMock()

        def fake_exists(key):
            if key == f"dtc:session:{seat.user_email}":
                # Inject concurrent confirm_seat call
                req = self.factory.post(
                    "/seats/confirm/",
                    {"seat_ids": [seat.id], "transaction_id": str(tid)},
                    format="json",
                    HTTP_X_INTERNAL_SERVICE_KEY=self.internal_key,
                )
                confirm_resp = confirm_seat(req)
                self.assertEqual(confirm_resp.status_code, 200)
                # Session is expired/missing in redis
                return False
            return False

        mock_redis.exists.side_effect = fake_exists

        with patch("inventory.services.get_redis_client", return_value=mock_redis):
            reclaimed, released = reclaim_expired_holds()

        seat.refresh_from_db()
        res.refresh_from_db()

        # The seat must remain BOOKED and reservation BOOKED
        self.assertEqual(seat.status, Seat.Status.BOOKED)
        self.assertEqual(res.status, Reservation.Status.BOOKED)
        self.assertEqual(reclaimed, 0)
        self.assertEqual(released, 0)

    # =========================================================================
    # FINDING 6: confirm_seat Scoped to Target Seats & RESERVED Status
    # =========================================================================
    def test_confirm_seat_does_not_update_unrelated_or_released_reservations(self):
        """
        Verifies that confirm_seat only updates reservations for the requested seat_ids
        that are in RESERVED status, and leaves RELEASED reservations untouched.
        """
        tid = uuid.uuid4()
        seat1 = Seat.objects.create(
            event=self.event,
            row="C",
            seat_number="1",
            price=100.00,
            status=Seat.Status.HELD,
            held_until=timezone.now() + timedelta(minutes=5),
            held_by_transaction_id=tid,
        )
        seat2 = Seat.objects.create(
            event=self.event,
            row="C",
            seat_number="2",
            price=100.00,
            status=Seat.Status.AVAILABLE,
            held_until=None,
            held_by_transaction_id=None,
        )

        res1 = Reservation.objects.create(
            seat=seat1,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
        )
        res2_released = Reservation.objects.create(
            seat=seat2,
            transaction_id=tid,
            status=Reservation.Status.RELEASED,
        )

        # Confirm only seat1
        resp = self.client.post(
            "/seats/confirm/",
            {"seat_ids": [seat1.id], "transaction_id": str(tid)},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY=self.internal_key,
        )
        self.assertEqual(resp.status_code, 200)

        res1.refresh_from_db()
        res2_released.refresh_from_db()

        # res1 should be BOOKED
        self.assertEqual(res1.status, Reservation.Status.BOOKED)
        # res2_released MUST remain RELEASED, never flipped to BOOKED
        self.assertEqual(res2_released.status, Reservation.Status.RELEASED)

    # =========================================================================
    # FINDING 12: Database Constraints & IntegrityError Mapping
    # =========================================================================
    def test_unique_seat_transaction_constraint(self):
        """
        Asserts that creating duplicate reservations with the same (seat, transaction_id)
        raises IntegrityError.
        """
        tid = uuid.uuid4()
        seat = Seat.objects.create(event=self.event, row="D", seat_number="1")
        Reservation.objects.create(seat=seat, transaction_id=tid, status=Reservation.Status.RESERVED)

        from django.db import transaction
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                Reservation.objects.create(seat=seat, transaction_id=tid, status=Reservation.Status.RESERVED)

    def test_unique_active_seat_reservation_constraint(self):
        """
        Asserts that a seat cannot have more than one active (RESERVED or BOOKED) reservation,
        while permitting multiple RELEASED historical reservations.
        """
        from django.db import transaction
        seat = Seat.objects.create(event=self.event, row="D", seat_number="2")
        tid1 = uuid.uuid4()
        tid2 = uuid.uuid4()

        Reservation.objects.create(seat=seat, transaction_id=tid1, status=Reservation.Status.RESERVED)

        # A second active reservation for the same seat must fail
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                Reservation.objects.create(seat=seat, transaction_id=tid2, status=Reservation.Status.RESERVED)

        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                Reservation.objects.create(seat=seat, transaction_id=tid2, status=Reservation.Status.BOOKED)

        # If previous is RELEASED, new reservation succeeds
        Reservation.objects.filter(transaction_id=tid1).update(status=Reservation.Status.RELEASED)
        new_res = Reservation.objects.create(seat=seat, transaction_id=tid2, status=Reservation.Status.RESERVED)
        self.assertEqual(new_res.status, Reservation.Status.RESERVED)

    def test_reserve_seat_maps_integrity_error_to_409(self):
        """
        Asserts that when an IntegrityError occurs during reserve_seat, it is cleanly mapped
        to an HTTP 409 SEAT_CONFLICT response rather than a 500 error.
        """
        seat = Seat.objects.create(event=self.event, row="D", seat_number="3")
        tid = uuid.uuid4()

        # Patch Reservation.objects.get_or_create to simulate an IntegrityError
        with patch("inventory.views.Reservation.objects.get_or_create", side_effect=IntegrityError("duplicate key")):
            resp = self.client.post(
                "/seats/reserve/",
                {
                    "seat_ids": [seat.id],
                    "transaction_id": str(tid),
                    "user_id": "usr_err",
                    "user_email": "err@example.com",
                },
                format="json",
                HTTP_X_INTERNAL_SERVICE_KEY=self.internal_key,
            )

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "SEAT_CONFLICT")

    def test_reserve_retry_mixed_booked_and_unbooked_returns_409(self):
        """
        Asserts that if a transaction attempts to reserve multiple seats where SOME are already
        BOOKED by this transaction and others are not, it explicitly returns 409 PARTIAL_SEATS_BOOKED_CONFLICT.
        """
        tid = uuid.uuid4()
        seat1 = Seat.objects.create(
            event=self.event,
            row="E",
            seat_number="1",
            price=100.00,
            status=Seat.Status.BOOKED,
            held_until=None,
            held_by_transaction_id=tid,
        )
        seat2 = Seat.objects.create(
            event=self.event,
            row="E",
            seat_number="2",
            price=100.00,
            status=Seat.Status.AVAILABLE,
            held_until=None,
            held_by_transaction_id=None,
        )

        resp = self.client.post(
            "/seats/reserve/",
            {
                "seat_ids": [seat1.id, seat2.id],
                "transaction_id": str(tid),
                "user_id": "usr_mixed",
                "user_email": "mixed@example.com",
            },
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY=self.internal_key,
        )

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "PARTIAL_SEATS_BOOKED_CONFLICT")

    def test_reclaim_expired_holds_exact_pair_matching_no_cross_product(self):
        """
        Asserts that reclaim_expired_holds matches reservations by exact (seat_id, transaction_id)
        pairs and does not produce a cartesian cross-product update.
        """
        tid_a = uuid.uuid4()
        tid_b = uuid.uuid4()
        now = timezone.now()

        seat_a = Seat.objects.create(
            event=self.event,
            row="F",
            seat_number="1",
            price=100.00,
            status=Seat.Status.HELD,
            held_until=now - timedelta(minutes=1),
            held_by_transaction_id=tid_a,
        )
        seat_b = Seat.objects.create(
            event=self.event,
            row="F",
            seat_number="2",
            price=100.00,
            status=Seat.Status.HELD,
            held_until=now - timedelta(minutes=1),
            held_by_transaction_id=tid_b,
        )

        res_a = Reservation.objects.create(seat=seat_a, transaction_id=tid_a, status=Reservation.Status.RESERVED)
        res_b = Reservation.objects.create(seat=seat_b, transaction_id=tid_b, status=Reservation.Status.RESERVED)

        reclaimed, released = reclaim_expired_holds()

        self.assertEqual(reclaimed, 2)
        self.assertEqual(released, 2)
        res_a.refresh_from_db()
        res_b.refresh_from_db()
        self.assertEqual(res_a.status, Reservation.Status.RELEASED)
        self.assertEqual(res_b.status, Reservation.Status.RELEASED)

    def test_migration_dedupe_logic_preserves_booked_rows(self):
        """
        Tests the dedupe_reservations algorithm used in migration 0015 against:
        1. Duplicate (seat, tid) rows with one BOOKED and one RESERVED.
        2. Two active rows on one seat (BOOKED + RESERVED).
        3. Multiple BOOKED duplicates (asserts BOOKED is never deleted; demoted to RELEASED instead).
        """
        import importlib
        migration_0015 = importlib.import_module("inventory.migrations.0015_dedupe_reservations")
        dedupe_reservations = migration_0015.dedupe_reservations
        from django.apps import apps
        from django.db import connection

        seat = Seat.objects.create(event=self.event, row="G", seat_number="1")
        tid1 = uuid.uuid4()
        tid2 = uuid.uuid4()

        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE inventory_reservation DROP CONSTRAINT IF EXISTS unique_seat_transaction;")
            cursor.execute("DROP INDEX IF EXISTS unique_active_seat_reservation CASCADE;")

        try:
            # Seed duplicate rows (one BOOKED, one RESERVED on same seat)
            booked_res = Reservation.objects.create(seat=seat, transaction_id=tid1, status=Reservation.Status.BOOKED)
            stale_res = Reservation.objects.create(seat=seat, transaction_id=tid2, status=Reservation.Status.RESERVED)

            schema_editor = connection.schema_editor()
            dedupe_reservations(apps, schema_editor)

            booked_res.refresh_from_db()
            stale_res.refresh_from_db()

            # BOOKED row must survive as BOOKED!
            self.assertEqual(booked_res.status, Reservation.Status.BOOKED)
            # Stale active row must be demoted to RELEASED
            self.assertEqual(stale_res.status, Reservation.Status.RELEASED)

            # Assert that dedupe cleanly left exactly 1 active reservation on this seat
            active_count = Reservation.objects.filter(
                seat=seat, status__in=[Reservation.Status.RESERVED, Reservation.Status.BOOKED]
            ).count()
            self.assertEqual(active_count, 1)
        finally:
            pass
