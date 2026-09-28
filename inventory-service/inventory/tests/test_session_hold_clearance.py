import uuid
from unittest.mock import patch, MagicMock
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIClient
from inventory.models import Event, Seat, Reservation
from inventory.views import reclaim_expired_holds


class SessionHoldClearanceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = Event.objects.create(
            name="Coldplay World Tour",
            venue="Grand Arena",
            starts_at=timezone.now() + timedelta(days=7),
        )
        self.seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="1",
            price="100.00",
            status=Seat.Status.HELD,
            held_until=timezone.now() + timedelta(minutes=5),
            held_by_transaction_id=uuid.uuid4(),
            user_id="usr_vijay",
            user_email="vijay@gmail.com",
        )
        self.reservation = Reservation.objects.create(
            seat=self.seat,
            transaction_id=self.seat.held_by_transaction_id,
            user_id="usr_vijay",
            user_email="vijay@gmail.com",
            status=Reservation.Status.RESERVED,
        )

    def test_held_seat_remains_held_when_redis_session_is_active(self):
        mock_redis = MagicMock()
        # Active session in Redis
        mock_redis.exists.return_value = 1

        with patch("inventory.views.get_redis_client", return_value=mock_redis):
            reclaimed, released = reclaim_expired_holds()
            self.assertEqual(reclaimed, 0)
            self.seat.refresh_from_db()
            self.assertEqual(self.seat.status, Seat.Status.HELD)
            mock_redis.exists.assert_called_with("dtc:session:vijay@gmail.com")

    def test_held_seat_reclaimed_when_redis_session_expires_on_exit(self):
        mock_redis = MagicMock()
        # Session expired / user closed browser
        mock_redis.exists.return_value = 0

        with patch("inventory.views.get_redis_client", return_value=mock_redis):
            reclaimed, released = reclaim_expired_holds()
            self.assertEqual(reclaimed, 1)
            self.assertEqual(released, 1)

            self.seat.refresh_from_db()
            self.assertEqual(self.seat.status, Seat.Status.AVAILABLE)
            self.assertIsNone(self.seat.held_until)
            self.assertIsNone(self.seat.held_by_transaction_id)
            self.assertIsNone(self.seat.user_id)
            self.assertIsNone(self.seat.user_email)

            self.reservation.refresh_from_db()
            self.assertEqual(self.reservation.status, Reservation.Status.RELEASED)
