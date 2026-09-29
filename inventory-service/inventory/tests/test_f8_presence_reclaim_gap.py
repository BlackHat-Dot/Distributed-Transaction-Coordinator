import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from inventory.models import Event, Reservation, Seat
from inventory.services import reclaim_expired_holds


class TestF8PresenceReclaimGap(TestCase):
    def setUp(self):
        self.event = Event.objects.create(
            name="F8 Presence Test Concert",
            venue="Arena",
            starts_at=timezone.now() + timedelta(days=5),
        )

    def test_seat_with_user_email_none_is_reclaimed_when_user_id_session_absent(self):
        """
        F8: When user_email is None, presence-based reclaim must fall back to dtc:session:{user_id}.
        If the session key is absent in Redis, the seat must be reclaimed (reclaimed == 1).
        """
        now = timezone.now()
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="F",
            seat_number="81",
            price="50.00",
            status=Seat.Status.HELD,
            # Hold created 5 minutes ago, expires in 5 minutes (well past 120s grace period)
            held_until=now + timedelta(minutes=5),
            held_by_transaction_id=tid,
            user_id="usr_no_email_absent",
            user_email=None,
        )
        Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            user_id="usr_no_email_absent",
            user_email=None,
            status=Reservation.Status.RESERVED,
        )

        mock_redis = MagicMock()
        mock_redis.pipeline.return_value.execute.return_value = [0, 0]
        mock_redis.exists.return_value = 0

        with patch("inventory.services.get_redis_client", return_value=mock_redis):
            reclaimed, released = reclaim_expired_holds()

        self.assertEqual(reclaimed, 1, "Seat with user_email=None and absent user_id session must be reclaimed")
        self.assertEqual(released, 1)
        seat.refresh_from_db()
        self.assertEqual(seat.status, Seat.Status.AVAILABLE)

    def test_seat_with_user_email_none_is_preserved_when_user_id_session_active(self):
        """
        F8: When user_email is None and dtc:session:{user_id} is active in Redis,
        the hold must NOT be reclaimed (reclaimed == 0).
        """
        now = timezone.now()
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="F",
            seat_number="82",
            price="50.00",
            status=Seat.Status.HELD,
            held_until=now + timedelta(minutes=5),
            held_by_transaction_id=tid,
            user_id="usr_no_email_active",
            user_email=None,
        )
        Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            user_id="usr_no_email_active",
            user_email=None,
            status=Reservation.Status.RESERVED,
        )

        mock_redis = MagicMock()
        # Active session for usr_no_email_active, no checkout key
        mock_redis.pipeline.return_value.execute.return_value = [1, 0]
        mock_redis.exists.side_effect = lambda k: 1 if k == "dtc:session:usr_no_email_active" else 0

        with patch("inventory.services.get_redis_client", return_value=mock_redis):
            reclaimed, released = reclaim_expired_holds()

        self.assertEqual(reclaimed, 0, "Seat with active user_id session must NOT be reclaimed")
        self.assertEqual(released, 0)
        seat.refresh_from_db()
        self.assertEqual(seat.status, Seat.Status.HELD)

    def test_seat_with_user_email_present_uses_email_key_primarily(self):
        """
        F8: When user_email is present, dtc:session:{email} is the primary presence key.
        """
        now = timezone.now()
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="F",
            seat_number="83",
            price="50.00",
            status=Seat.Status.HELD,
            held_until=now + timedelta(minutes=5),
            held_by_transaction_id=tid,
            user_id="usr_with_email",
            user_email="f8user@example.com",
        )
        Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            user_id="usr_with_email",
            user_email="f8user@example.com",
            status=Reservation.Status.RESERVED,
        )

        checked_keys = []
        mock_redis = MagicMock()

        def fake_exists(k):
            checked_keys.append(k)
            return 1 if k == "dtc:session:f8user@example.com" else 0

        mock_redis.pipeline.return_value.execute.return_value = [1, 0]
        mock_redis.exists.side_effect = fake_exists

        with patch("inventory.services.get_redis_client", return_value=mock_redis):
            reclaimed, released = reclaim_expired_holds()

        self.assertEqual(reclaimed, 0)
        seat.refresh_from_db()
        self.assertEqual(seat.status, Seat.Status.HELD)
        # Verify dtc:session:f8user@example.com was checked
        pipe_calls = [c[0][0] for c in mock_redis.pipeline.return_value.exists.call_args_list]
        self.assertIn("dtc:session:f8user@example.com", pipe_calls)
