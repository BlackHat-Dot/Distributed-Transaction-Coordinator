import uuid
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from inventory.models import Event, Seat, Reservation


class TestC1PricingErrorMessage(TestCase):
    def test_pricing_error_message_uses_seat_ids(self):
        """
        C1: Error message for invalid seat_ids in seat_pricing must say 'seat_ids',
        not the typo 'seaSeatSerializert_ids'.
        """
        client = APIClient()
        resp = client.post(
            "/seats/pricing/",
            {"seat_ids": ["invalid_str"]},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY=getattr(settings, "INTERNAL_SERVICE_KEY", "dtc-internal-coordinator-secret-key-2026"),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("seat_ids", resp.data.get("error", ""))
        self.assertNotIn("seaSeatSerializert_ids", resp.data.get("error", ""))


class TestC2CleanCode(TestCase):
    def test_internal_reserve_missing_tid_returns_400(self):
        """
        C2: Internal reserve call requires transaction_id, returns 400 if omitted.
        """
        client = APIClient()
        event = Event.objects.create(name="C2 Event")
        seat = Seat.objects.create(event=event, row="A", seat_number="1", status=Seat.Status.AVAILABLE)
        resp = client.post(
            "/seats/reserve/",
            {"seat_ids": [seat.id]},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY=getattr(settings, "INTERNAL_SERVICE_KEY", "dtc-internal-coordinator-secret-key-2026"),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("error"), "transaction_id is required")


class TestC3MaxUserHolds(TestCase):
    @override_settings(MAX_USER_HOLDS=None)
    def test_max_user_holds_none_allows_more_than_ten_holds(self):
        """
        C3: When MAX_USER_HOLDS is None (unlimited), reserving 12 seats must be allowed (not capped at 10).
        """
        client = APIClient()
        event = Event.objects.create(name="C3 Event")
        seats = [
            Seat.objects.create(event=event, row="H", seat_number=str(i), status=Seat.Status.AVAILABLE, price=10.0)
            for i in range(1, 13)
        ]
        resp = client.post(
            "/seats/reserve/",
            {"seat_ids": [s.id for s in seats], "transaction_id": str(uuid.uuid4())},
            format="json",
            HTTP_X_USER_ID="usr_c3_unlimited",
            HTTP_X_USER_EMAIL="c3_unlimited@example.com",
        )
        self.assertEqual(resp.status_code, 200, f"Expected 200 when MAX_USER_HOLDS=None, got {resp.status_code}: {resp.data}")

    @override_settings(MAX_USER_HOLDS=0)
    def test_max_user_holds_zero_disables_holds(self):
        """
        C3: When MAX_USER_HOLDS is explicitly 0, any hold request must be rejected with 429.
        """
        client = APIClient()
        event = Event.objects.create(name="C3 Event 2")
        seat = Seat.objects.create(event=event, row="H", seat_number="99", status=Seat.Status.AVAILABLE, price=10.0)
        resp = client.post(
            "/seats/reserve/",
            {"seat_ids": [seat.id], "transaction_id": str(uuid.uuid4())},
            format="json",
            HTTP_X_USER_ID="usr_c3_zero",
            HTTP_X_USER_EMAIL="c3_zero@example.com",
        )
        self.assertEqual(resp.status_code, 429, f"Expected 429 when MAX_USER_HOLDS=0, got {resp.status_code}")


class TestC4SeatListEventFilter(TestCase):
    def test_seat_list_filters_by_event_id(self):
        """
        C4: seat_list with ?event_id=X must only return seats for event X.
        """
        client = APIClient()
        event1 = Event.objects.create(name="C4 Event 1")
        event2 = Event.objects.create(name="C4 Event 2")

        seat1_e1 = Seat.objects.create(event=event1, row="A", seat_number="1", status=Seat.Status.AVAILABLE)
        seat2_e1 = Seat.objects.create(event=event1, row="A", seat_number="2", status=Seat.Status.AVAILABLE)
        seat1_e2 = Seat.objects.create(event=event2, row="B", seat_number="1", status=Seat.Status.AVAILABLE)
        seat2_e2 = Seat.objects.create(event=event2, row="B", seat_number="2", status=Seat.Status.AVAILABLE)

        resp = client.get(f"/seats/?event_id={event1.id}")
        self.assertEqual(resp.status_code, 200)
        seat_ids = [s["id"] for s in resp.data]
        self.assertEqual(len(seat_ids), 2)
        self.assertIn(seat1_e1.id, seat_ids)
        self.assertIn(seat2_e1.id, seat_ids)
        self.assertNotIn(seat1_e2.id, seat_ids)
        self.assertNotIn(seat2_e2.id, seat_ids)

    def test_seed_demo_seats_command(self):
        """
        C4: Management command seed_demo_seats populates events and demo seats.
        """
        from django.core.management import call_command
        # Clear existing seats to test seeding
        Seat.objects.all().delete()
        Event.objects.all().delete()
        call_command("seed_demo_seats")
        self.assertEqual(Event.objects.count(), 1)
        self.assertEqual(Seat.objects.count(), 30)


class TestC5RedisNegativeCaching(TestCase):
    def test_redis_connection_failure_negative_cached_for_five_seconds(self):
        """
        C5: When Redis connection fails, subsequent calls within 5s return None
        immediately without attempting to reconnect or paying timeout penalty.
        """
        import inventory.views as views
        # Reset globals
        views._redis_client_instance = None
        if hasattr(views, "_redis_last_failure"):
            views._redis_last_failure = 0.0

        with patch("redis.Redis.from_url", side_effect=Exception("Connection refused")) as mock_redis:
            client1 = views.get_redis_client()
            self.assertIsNone(client1)
            self.assertEqual(mock_redis.call_count, 1)

            # Second call immediately after should be negatively cached
            client2 = views.get_redis_client()
            self.assertIsNone(client2)
            self.assertEqual(mock_redis.call_count, 1, "Redis reconnect should not be attempted within TTL")


class TestC6EmailNormalization(TestCase):
    def test_reclaim_normalizes_email_to_lowercase_for_presence_check(self):
        """
        C6: If seat has uppercase email, presence check must check dtc:session:{lowercase}
        so an active session with lowercase key prevents premature reclaim.
        """
        import inventory.views as views
        from datetime import timedelta
        event = Event.objects.create(name="C6 Event")
        now = timezone.now()
        tid = uuid.uuid4()

        # Hold started 3 minutes ago, held_until is 7 minutes in future (age = 180s >= 120s grace)
        held_until = now + timedelta(minutes=7)
        seat = Seat.objects.create(
            event=event,
            row="I",
            seat_number="1",
            status=Seat.Status.HELD,
            held_until=held_until,
            held_by_transaction_id=tid,
            user_id="usr_c6",
            user_email="ALICE@EXAMPLE.COM",  # uppercase in DB
            price=100.0,
        )
        Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
            user_id="usr_c6",
            user_email="ALICE@EXAMPLE.COM",
        )

        class MockPipeline:
            def __init__(self):
                self.keys = []
            def exists(self, key):
                self.keys.append(key)
                return self
            def execute(self):
                # Only lowercase session key exists!
                return [1 if k == "dtc:session:alice@example.com" else 0 for k in self.keys]

        class MockRedis:
            def pipeline(self):
                return MockPipeline()
            def exists(self, key):
                return 1 if key == "dtc:session:alice@example.com" else 0

        with patch("inventory.views.get_redis_client", return_value=MockRedis()):
            reclaimed, released = views.reclaim_expired_holds()

        # In un-fixed code, checked dtc:session:ALICE@EXAMPLE.COM -> 0 -> reclaimed == 1
        # In fixed code, checks dtc:session:alice@example.com -> 1 -> reclaimed == 0
        self.assertEqual(reclaimed, 0, "Active lowercase session must protect uppercase email hold from reclaim")
        seat.refresh_from_db()
        self.assertEqual(seat.status, Seat.Status.HELD)
