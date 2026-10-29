from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
import uuid

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from inventory.models import Event, Seat, Reservation
from inventory.services import reclaim_expired_holds


class HoldSweeperAndAdminTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        cache.clear()
        self.internal_key = getattr(settings, "INTERNAL_SERVICE_KEY", "test-internal-key")
        self.staff_key = getattr(settings, "STAFF_API_KEY", "test-staff-key")
        self.internal_headers = {"HTTP_X_INTERNAL_SERVICE_KEY": self.internal_key}
        self.staff_headers = {"HTTP_X_STAFF_KEY": self.staff_key}

        self.event = Event.objects.create(
            name="Mega Festival 2026",
            venue="Grand Arena",
            starts_at=timezone.now() + timedelta(days=20),
        )

        self.seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="1",
            price=Decimal("150.00"),
            status=Seat.Status.AVAILABLE,
        )

        self.tx_id = uuid.uuid4()
        self.user_email = "shopper@gmail.com"

    def tearDown(self):
        cache.clear()

    def test_reclaim_expired_holds_time_based(self):
        """Expired holds (held_until <= now) revert to AVAILABLE with released_reason EXPIRED_HOLD."""
        # Seat held in past (expired)
        self.seat.status = Seat.Status.HELD
        self.seat.held_by_transaction_id = self.tx_id
        self.seat.held_until = timezone.now() - timedelta(minutes=1)
        self.seat.save()

        Reservation.objects.create(
            seat=self.seat,
            transaction_id=self.tx_id,
            status=Reservation.Status.RESERVED,
        )

        reclaimed, released = reclaim_expired_holds()
        self.assertEqual(reclaimed, 1)
        self.assertEqual(released, 1)

        self.seat.refresh_from_db()
        self.assertEqual(self.seat.status, Seat.Status.AVAILABLE)
        self.assertIsNone(self.seat.held_until)
        self.assertIsNone(self.seat.held_by_transaction_id)

        res = Reservation.objects.get(transaction_id=self.tx_id)
        self.assertEqual(res.status, Reservation.Status.RELEASED)
        self.assertEqual(res.released_reason, "EXPIRED_HOLD")

    def test_future_holds_not_reclaimed_by_time_sweep(self):
        """Active holds with future held_until are preserved by the time-based sweeper."""
        self.seat.status = Seat.Status.HELD
        self.seat.held_by_transaction_id = self.tx_id
        self.seat.held_until = timezone.now() + timedelta(minutes=8)
        self.seat.save()

        Reservation.objects.create(
            seat=self.seat,
            transaction_id=self.tx_id,
            status=Reservation.Status.RESERVED,
        )

        reclaimed, released = reclaim_expired_holds()
        self.assertEqual(reclaimed, 0)
        self.assertEqual(released, 0)

        self.seat.refresh_from_db()
        self.assertEqual(self.seat.status, Seat.Status.HELD)

    def test_reclaim_expired_holds_presence_abandonment(self):
        """User abandonment: hold age > grace period and absence of Redis session reclaims hold."""
        # Hold was established 4 minutes ago (held_until = now + 6m)
        self.seat.status = Seat.Status.HELD
        self.seat.held_by_transaction_id = self.tx_id
        self.seat.user_email = self.user_email
        self.seat.held_until = timezone.now() + timedelta(minutes=6)
        self.seat.save()

        Reservation.objects.create(
            seat=self.seat,
            transaction_id=self.tx_id,
            user_email=self.user_email,
            status=Reservation.Status.RESERVED,
        )

        # Mock Redis client where session key does NOT exist (user closed browser)
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        # exists(session_key) -> 0, exists(checkout_key) -> 0
        mock_pipe.execute.return_value = [0, 0]

        with patch("inventory.services._get_active_redis_client", return_value=mock_redis), \
             self.settings(HOLD_PRESENCE_GRACE_SECONDS=120, HOLD_DURATION_MINUTES=10):
            reclaimed, released = reclaim_expired_holds()
            self.assertEqual(reclaimed, 1)
            self.assertEqual(released, 1)

        self.seat.refresh_from_db()
        self.assertEqual(self.seat.status, Seat.Status.AVAILABLE)

    def test_checkout_presence_key_shields_active_payment(self):
        """When dtc:checkout:{tx_id} exists in Redis, presence-based reclaim is skipped."""
        # Hold age > grace period, but user is currently paying
        self.seat.status = Seat.Status.HELD
        self.seat.held_by_transaction_id = self.tx_id
        self.seat.user_email = self.user_email
        self.seat.held_until = timezone.now() + timedelta(minutes=6)
        self.seat.save()

        Reservation.objects.create(
            seat=self.seat,
            transaction_id=self.tx_id,
            user_email=self.user_email,
            status=Reservation.Status.RESERVED,
        )

        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        # exists(session_key) -> 0 (lapsed), exists(checkout_key) -> 1 (checkout in-flight!)
        mock_pipe.execute.return_value = [0, 1]

        with patch("inventory.services._get_active_redis_client", return_value=mock_redis), \
             self.settings(HOLD_PRESENCE_GRACE_SECONDS=120, HOLD_DURATION_MINUTES=10):
            reclaimed, released = reclaim_expired_holds()
            self.assertEqual(reclaimed, 0)
            self.assertEqual(released, 0)

        self.seat.refresh_from_db()
        self.assertEqual(self.seat.status, Seat.Status.HELD)

    def test_cas_race_prevents_reclaiming_reheld_seat(self):
        """If a seat is re-held by another transaction after snapshot, sweeper CAS aborts."""
        self.seat.status = Seat.Status.HELD
        self.seat.held_by_transaction_id = self.tx_id
        self.seat.held_until = timezone.now() - timedelta(minutes=1)
        self.seat.save()

        new_tx_id = uuid.uuid4()

        # Simulate concurrent re-hold occurring between query and lock
        def fake_select_for_update():
            # Update seat held_by_transaction_id to new_tx_id
            Seat.objects.filter(id=self.seat.id).update(
                held_by_transaction_id=new_tx_id,
                held_until=timezone.now() + timedelta(minutes=10),
            )
            return Seat.objects.filter(id=self.seat.id)

        with patch.object(Seat.objects, "select_for_update", side_effect=fake_select_for_update):
            reclaimed, released = reclaim_expired_holds()
            self.assertEqual(reclaimed, 0)

        self.seat.refresh_from_db()
        self.assertEqual(self.seat.held_by_transaction_id, new_tx_id)

    def test_sweep_expired_holds_management_command(self):
        """Management command sweep_expired_holds triggers reclaim_expired_holds cleanly."""
        self.seat.status = Seat.Status.HELD
        self.seat.held_by_transaction_id = self.tx_id
        self.seat.held_until = timezone.now() - timedelta(minutes=2)
        self.seat.save()

        call_command("sweep_expired_holds")

        self.seat.refresh_from_db()
        self.assertEqual(self.seat.status, Seat.Status.AVAILABLE)

    def test_sweep_expired_holds_api_endpoint(self):
        """POST /seats/sweep-expired/ requires service key and sweeps expired holds."""
        self.seat.status = Seat.Status.HELD
        self.seat.held_by_transaction_id = self.tx_id
        self.seat.held_until = timezone.now() - timedelta(minutes=2)
        self.seat.save()

        # Without credentials: 403
        with self.settings(IS_TESTING=False):
            resp = self.client.post("/seats/sweep-expired/")
            self.assertEqual(resp.status_code, 403)

        # With internal key: 200
        resp = self.client.post("/seats/sweep-expired/", **self.internal_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("reclaimed_count"), 1)

    def test_reset_seats_admin_action_marks_booked_as_admin_reset(self):
        """POST /seats/reset/ resets seats and marks BOOKED reservations with released_reason ADMIN_RESET."""
        self.seat.status = Seat.Status.BOOKED
        self.seat.held_by_transaction_id = self.tx_id
        self.seat.save()

        Reservation.objects.create(
            seat=self.seat,
            transaction_id=self.tx_id,
            status=Reservation.Status.BOOKED,
        )

        with self.settings(DEBUG=False, ALLOW_UNAUTHENTICATED_RESET=False):
            # 1. Missing staff key -> 403
            resp = self.client.post("/seats/reset/", {"event_id": self.event.id}, format="json")
            self.assertEqual(resp.status_code, 403)

            # 2. With staff key -> 200
            resp = self.client.post(
                "/seats/reset/",
                {"event_id": self.event.id},
                format="json",
                **self.staff_headers,
            )
            self.assertEqual(resp.status_code, 200)

        self.seat.refresh_from_db()
        self.assertEqual(self.seat.status, Seat.Status.AVAILABLE)

        # Crucial check: BOOKED reservations must not be deleted; marked RELEASED with ADMIN_RESET
        res = Reservation.objects.get(transaction_id=self.tx_id)
        self.assertEqual(res.status, Reservation.Status.RELEASED)
        self.assertEqual(res.released_reason, "ADMIN_RESET")

    def test_seat_pricing_endpoint_lookups(self):
        """POST /seats/pricing/ returns accurate price records for internal coordinator resolution."""
        payload = {"seat_ids": [self.seat.id]}

        # Valid lookup
        resp = self.client.post("/seats/pricing/", payload, format="json", **self.internal_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["id"], self.seat.id)
        self.assertEqual(resp.data[0]["price"], "150.00")

        # Invalid format lookup
        resp = self.client.post("/seats/pricing/", {"seat_ids": "not-a-list"}, format="json", **self.internal_headers)
        self.assertEqual(resp.status_code, 400)

    def test_event_and_seat_catalog_queries(self):
        """GET /events/ and GET /seats/ return correct event and seat catalogs with status presentation."""
        # Query events
        resp = self.client.get("/events/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(e["id"] == self.event.id for e in resp.data))

        # Query seats with event filter
        resp = self.client.get(f"/seats/?event_id={self.event.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(s["id"] == self.seat.id for s in resp.data))

    def test_ready_check_dependencies(self):
        """GET /ready/ verifies database and redis connectivity, returning 200 when healthy."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True

        with patch("inventory.views.get_redis_client", return_value=mock_redis):
            resp = self.client.get("/ready/")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data.get("status"), "ready")
            self.assertEqual(resp.data.get("database"), "connected")
            self.assertEqual(resp.data.get("redis"), "connected")
