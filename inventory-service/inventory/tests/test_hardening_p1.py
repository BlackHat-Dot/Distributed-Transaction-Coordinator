import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from inventory.models import Event, Seat, Reservation
from inventory.views import reclaim_expired_holds


class TestB1ReclaimRaceCondition(TestCase):
    def test_b1_reclaim_skips_freshly_extended_hold_with_same_tid(self):
        """
        B1: If reserve_seat is idempotently retried with the SAME transaction_id
        between reclaim's snapshot and lock phases, extending held_until into the future,
        reclaim MUST NOT reclaim the freshly extended seat.
        """
        event = Event.objects.create(name="B1 Concert")
        tid = uuid.uuid4()
        now = timezone.now()

        # Seat hold originally expired 10 seconds ago
        original_held_until = now - timedelta(seconds=10)
        seat = Seat.objects.create(
            event=event,
            row="A",
            seat_number="1",
            status=Seat.Status.HELD,
            held_until=original_held_until,
            held_by_transaction_id=tid,
            user_id="usr_b1",
            user_email="b1@example.com",
        )
        Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
            user_id="usr_b1",
            user_email="b1@example.com",
        )

        def simulate_idempotent_retry_interleaving():
            # Interleaving: An idempotent retry with the same tid extends held_until
            Seat.objects.filter(id=seat.id).update(
                held_until=timezone.now() + timedelta(minutes=10)
            )
            return None

        # Hook get_redis_client, which runs right after Seat.objects.filter(...).values(...) snapshot
        with patch("inventory.views.get_redis_client", side_effect=simulate_idempotent_retry_interleaving):
            reclaimed, released = reclaim_expired_holds()

        # In un-fixed code, reclaimed == 1 because held_by_transaction_id matched tid.
        # In fixed code, reclaimed must be 0 because held_until was extended into the future.
        self.assertEqual(reclaimed, 0, "Freshly extended hold must not be reclaimed by sweep")
        self.assertEqual(released, 0)
        seat.refresh_from_db()
        self.assertEqual(seat.status, Seat.Status.HELD)
        self.assertTrue(seat.held_until > timezone.now())


class TestB2HoldDurationHelper(TestCase):
    @override_settings(HOLD_DURATION_MINUTES=5)
    def test_b2_hold_duration_setting_respected(self):
        from rest_framework.test import APIClient
        client = APIClient()
        event = Event.objects.create(name="B2 Event")
        seat = Seat.objects.create(
            event=event, row="B", seat_number="1", status=Seat.Status.AVAILABLE, price=50.00
        )
        tid = str(uuid.uuid4())
        resp = client.post(
            "/seats/reserve/",
            {"seat_ids": [seat.id], "transaction_id": tid},
            format="json",
            HTTP_X_USER_ID="usr_b2",
            HTTP_X_USER_EMAIL="b2@example.com",
        )
        self.assertEqual(resp.status_code, 200)
        seat.refresh_from_db()
        now = timezone.now()
        remaining_seconds = (seat.held_until - now).total_seconds()
        self.assertLessEqual(
            remaining_seconds, 305, f"Expected hold duration <= 305s (5m), got {remaining_seconds}s"
        )
        self.assertGreaterEqual(remaining_seconds, 290)


class TestB3ConfirmReservationDesync(TestCase):
    def test_b3_confirm_aborts_and_rolls_back_if_reservation_missing(self):
        """
        B3: Fabricate scenario where 2 seats are held for a transaction,
        but only 1 Reservation row exists (the other was deleted/missing).
        Confirm must fail with 409 RESERVATION_STATE_MISMATCH and roll back both seats.
        """
        from rest_framework.test import APIClient
        from django.conf import settings
        client = APIClient()
        event = Event.objects.create(name="B3 Event")
        now = timezone.now()
        tid = uuid.uuid4()
        seat1 = Seat.objects.create(
            event=event,
            row="C",
            seat_number="1",
            status=Seat.Status.HELD,
            held_until=now + timedelta(minutes=10),
            held_by_transaction_id=tid,
        )
        seat2 = Seat.objects.create(
            event=event,
            row="C",
            seat_number="2",
            status=Seat.Status.HELD,
            held_until=now + timedelta(minutes=10),
            held_by_transaction_id=tid,
        )
        # Only create Reservation for seat1; seat2 reservation is missing
        Reservation.objects.create(
            seat=seat1,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
        )

        resp = client.post(
            "/seats/confirm/",
            {"seat_ids": [seat1.id, seat2.id], "transaction_id": str(tid)},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY=getattr(settings, "INTERNAL_SERVICE_KEY", "dtc-internal-coordinator-secret-key-2026"),
        )

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "RESERVATION_STATE_MISMATCH")

        # Verify rollback: neither seat should be BOOKED
        seat1.refresh_from_db()
        seat2.refresh_from_db()
        self.assertEqual(seat1.status, Seat.Status.HELD)
        self.assertEqual(seat2.status, Seat.Status.HELD)

    def test_b3_confirm_idempotent_retry_when_both_booked(self):
        """
        Idempotent retry with both seats already BOOKED and reservations BOOKED succeeds with 200.
        """
        from rest_framework.test import APIClient
        from django.conf import settings
        client = APIClient()
        event = Event.objects.create(name="B3 Idempotent Event")
        tid = uuid.uuid4()
        seat1 = Seat.objects.create(
            event=event, row="C", seat_number="3", status=Seat.Status.BOOKED, held_by_transaction_id=tid
        )
        Reservation.objects.create(
            seat=seat1, transaction_id=tid, status=Reservation.Status.BOOKED
        )
        resp = client.post(
            "/seats/confirm/",
            {"seat_ids": [seat1.id], "transaction_id": str(tid)},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY=getattr(settings, "INTERNAL_SERVICE_KEY", "dtc-internal-coordinator-secret-key-2026"),
        )
        self.assertEqual(resp.status_code, 200)


class TestB4InputValidation(TestCase):
    def test_non_dict_body_returns_400(self):
        from rest_framework.test import APIClient
        client = APIClient()
        resp = client.post(
            "/seats/reserve/",
            [1, 2, 3],
            format="json",
            HTTP_X_USER_ID="usr_b4",
            HTTP_X_USER_EMAIL="b4@example.com",
        )
        self.assertEqual(resp.status_code, 400)

    def test_seat_ids_with_boolean_returns_400(self):
        from rest_framework.test import APIClient
        client = APIClient()
        resp = client.post(
            "/seats/reserve/",
            {"seat_ids": [True, 1], "transaction_id": str(uuid.uuid4())},
            format="json",
            HTTP_X_USER_ID="usr_b4",
            HTTP_X_USER_EMAIL="b4@example.com",
        )
        self.assertEqual(resp.status_code, 400)

    def test_seat_ids_with_float_returns_400(self):
        from rest_framework.test import APIClient
        client = APIClient()
        resp = client.post(
            "/seats/reserve/",
            {"seat_ids": [1.5], "transaction_id": str(uuid.uuid4())},
            format="json",
            HTTP_X_USER_ID="usr_b4",
            HTTP_X_USER_EMAIL="b4@example.com",
        )
        self.assertEqual(resp.status_code, 400)


class TestB5ReservePreservesAuditHistory(TestCase):
    def test_reassign_expired_hold_marks_old_reservation_released_not_deleted(self):
        """
        B5: Reassigning an expired hold to a new transaction must NOT hard-delete
        the old Reservation row; it must mark it as RELEASED for audit history.
        """
        from rest_framework.test import APIClient
        client = APIClient()
        event = Event.objects.create(name="B5 Event")
        now = timezone.now()
        old_tid = uuid.uuid4()
        new_tid = uuid.uuid4()

        seat = Seat.objects.create(
            event=event,
            row="D",
            seat_number="1",
            status=Seat.Status.HELD,
            held_until=now - timedelta(seconds=10),  # expired hold
            held_by_transaction_id=old_tid,
            user_id="usr_b5_old",
            user_email="b5_old@example.com",
            price=100.00,
        )
        old_res = Reservation.objects.create(
            seat=seat,
            transaction_id=old_tid,
            status=Reservation.Status.RESERVED,
            user_id="usr_b5_old",
            user_email="b5_old@example.com",
        )

        resp = client.post(
            "/seats/reserve/",
            {"seat_ids": [seat.id], "transaction_id": str(new_tid)},
            format="json",
            HTTP_X_USER_ID="usr_b5_new",
            HTTP_X_USER_EMAIL="b5_new@example.com",
        )
        self.assertEqual(resp.status_code, 200)

        # Old reservation must NOT be deleted
        old_res.refresh_from_db()
        self.assertEqual(old_res.status, Reservation.Status.RELEASED)

        # New reservation exists and is RESERVED
        new_res = Reservation.objects.get(transaction_id=new_tid, seat=seat)
        self.assertEqual(new_res.status, Reservation.Status.RESERVED)


class TestB6UserIdNormalization(TestCase):
    def test_same_user_transition_with_integer_user_id_succeeds(self):
        """
        B6: If user_id comes in as an integer (e.g. 12345), but seat.user_id in DB
        is stored as string '12345', same-user transition must succeed (200 OK)
        and not fail with 409 SEAT_UNAVAILABLE due to type inequality.
        """
        from rest_framework.test import APIClient
        from django.conf import settings
        client = APIClient()
        event = Event.objects.create(name="B6 Event")
        now = timezone.now()
        old_tid = uuid.uuid4()
        new_tid = uuid.uuid4()

        seat = Seat.objects.create(
            event=event,
            row="E",
            seat_number="1",
            status=Seat.Status.HELD,
            held_until=now + timedelta(minutes=10),
            held_by_transaction_id=old_tid,
            user_id="12345",
            user_email="user12345@example.com",
            price=100.00,
        )
        Reservation.objects.create(
            seat=seat,
            transaction_id=old_tid,
            status=Reservation.Status.RESERVED,
            user_id="12345",
            user_email="user12345@example.com",
        )

        resp = client.post(
            "/seats/reserve/",
            {
                "seat_ids": [seat.id],
                "transaction_id": str(new_tid),
                "user_id": 12345,  # int in request body
                "user_email": "user12345@example.com",
            },
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY=getattr(settings, "INTERNAL_SERVICE_KEY", "dtc-internal-coordinator-secret-key-2026"),
        )
        self.assertEqual(resp.status_code, 200, f"Expected 200 for same-user transition with int user_id, got {resp.status_code}: {resp.data}")
