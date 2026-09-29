import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
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
