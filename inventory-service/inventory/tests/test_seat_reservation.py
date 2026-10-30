from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
import uuid

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, SimpleTestCase
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from inventory.models import Event, Seat, Reservation
from inventory.throttling import (
    AnonIpRateThrottle,
    CheckReservationRateThrottle,
    SeatListRateThrottle,
    UserActionRateThrottle,
)


class SeatReservationLifecycleTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        cache.clear()
        self.internal_key = getattr(settings, "INTERNAL_SERVICE_KEY", "test-internal-key")
        self.headers = {"HTTP_X_INTERNAL_SERVICE_KEY": self.internal_key}

        self.event = Event.objects.create(
            name="Rock Live 2026",
            venue="Grand Arena",
            starts_at=timezone.now() + timedelta(days=14),
        )

        self.seat1 = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="1",
            price=Decimal("100.00"),
            status=Seat.Status.AVAILABLE,
        )
        self.seat2 = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="2",
            price=Decimal("120.00"),
            status=Seat.Status.AVAILABLE,
        )

        self.user_id = "usr_inv_tester"
        self.user_email = "shopper@gmail.com"
        self.tx_id = uuid.uuid4()

    def tearDown(self):
        cache.clear()

    def test_reserve_seat_success_and_pricing_calculation(self):
        """Available seats are atomically held with 10-minute hold expiry and RESERVED reservation record."""
        payload = {
            "seat_ids": [self.seat1.id, self.seat2.id],
            "transaction_id": str(self.tx_id),
            "user_id": self.user_id,
            "user_email": self.user_email,
        }
        resp = self.client.post("/seats/reserve/", payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["transaction_id"], str(self.tx_id))
        self.assertEqual(resp.data["total_amount"], "220.00")
        self.assertIsNotNone(resp.data["held_until"])

        # Verify DB Seat status
        self.seat1.refresh_from_db()
        self.seat2.refresh_from_db()
        self.assertEqual(self.seat1.status, Seat.Status.HELD)
        self.assertEqual(self.seat1.held_by_transaction_id, self.tx_id)
        self.assertEqual(self.seat1.user_id, self.user_id)
        self.assertEqual(self.seat2.status, Seat.Status.HELD)

        # Verify Reservation table
        reservations = Reservation.objects.filter(transaction_id=self.tx_id)
        self.assertEqual(reservations.count(), 2)
        for r in reservations:
            self.assertEqual(r.status, Reservation.Status.RESERVED)

    def test_reserve_seat_rejects_already_held_seat(self):
        """Seat currently held by another user cannot be reserved, returns 409 SEAT_UNAVAILABLE."""
        # Seat 1 is held by active user
        self.seat1.status = Seat.Status.HELD
        self.seat1.held_by_transaction_id = self.tx_id
        self.seat1.held_until = timezone.now() + timedelta(minutes=5)
        self.seat1.user_id = self.user_id
        self.seat1.save()

        # Another user tries to reserve Seat 1
        other_tx = uuid.uuid4()
        payload = {
            "seat_ids": [self.seat1.id],
            "transaction_id": str(other_tx),
            "user_id": "usr_another",
            "user_email": "another@gmail.com",
        }
        resp = self.client.post("/seats/reserve/", payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "SEAT_UNAVAILABLE")

    def test_reserve_seat_reclaims_expired_hold_for_new_user(self):
        """If a seat's held_until has lapsed, a new user can immediately take over the seat."""
        # Hold expired 2 minutes ago
        self.seat1.status = Seat.Status.HELD
        self.seat1.held_by_transaction_id = uuid.uuid4()
        self.seat1.held_until = timezone.now() - timedelta(minutes=2)
        self.seat1.user_id = "usr_expired"
        self.seat1.save()

        payload = {
            "seat_ids": [self.seat1.id],
            "transaction_id": str(self.tx_id),
            "user_id": self.user_id,
            "user_email": self.user_email,
        }
        resp = self.client.post("/seats/reserve/", payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 200)

        self.seat1.refresh_from_db()
        self.assertEqual(self.seat1.held_by_transaction_id, self.tx_id)
        self.assertEqual(self.seat1.user_id, self.user_id)
        self.assertGreater(self.seat1.held_until, timezone.now())

    def test_reserve_seat_user_quota_limit(self):
        """User holding max allowed seats is blocked from holding more with 429 MAX_HOLDS_EXCEEDED."""
        max_holds = 2
        with self.settings(MAX_USER_HOLDS=max_holds):
            # Create extra seats and hold them for this user
            extra_seat1 = Seat.objects.create(
                event=self.event, row="B", seat_number="1", price=Decimal("50.00"),
                status=Seat.Status.HELD, held_until=timezone.now() + timedelta(minutes=5),
                held_by_transaction_id=uuid.uuid4(), user_id=self.user_id
            )
            extra_seat2 = Seat.objects.create(
                event=self.event, row="B", seat_number="2", price=Decimal("50.00"),
                status=Seat.Status.HELD, held_until=timezone.now() + timedelta(minutes=5),
                held_by_transaction_id=uuid.uuid4(), user_id=self.user_id
            )

            # Attempt to reserve one more seat
            payload = {
                "seat_ids": [self.seat1.id],
                "transaction_id": str(self.tx_id),
                "user_id": self.user_id,
                "user_email": self.user_email,
            }
            resp = self.client.post("/seats/reserve/", payload, format="json", **self.headers)
            self.assertEqual(resp.status_code, 429)
            self.assertEqual(resp.data.get("code"), "MAX_HOLDS_EXCEEDED")

    def test_confirm_seat_success_transitions_to_booked(self):
        """Valid held seats confirm to BOOKED, clearing held_until and marking reservation BOOKED."""
        # Set up active hold
        self.seat1.status = Seat.Status.HELD
        self.seat1.held_by_transaction_id = self.tx_id
        self.seat1.held_until = timezone.now() + timedelta(minutes=5)
        self.seat1.user_id = self.user_id
        self.seat1.save()

        Reservation.objects.create(
            seat=self.seat1,
            transaction_id=self.tx_id,
            user_id=self.user_id,
            user_email=self.user_email,
            status=Reservation.Status.RESERVED,
        )

        confirm_payload = {
            "seat_ids": [self.seat1.id],
            "transaction_id": str(self.tx_id),
        }
        resp = self.client.post("/seats/confirm/", confirm_payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("confirmed", resp.data.get("message"))

        self.seat1.refresh_from_db()
        self.assertEqual(self.seat1.status, Seat.Status.BOOKED)
        self.assertIsNone(self.seat1.held_until)

        res = Reservation.objects.get(transaction_id=self.tx_id, seat=self.seat1)
        self.assertEqual(res.status, Reservation.Status.BOOKED)

    def test_confirm_seat_rejects_expired_hold(self):
        """Confirming an expired hold fails with 409 HOLD_EXPIRED."""
        self.seat1.status = Seat.Status.HELD
        self.seat1.held_by_transaction_id = self.tx_id
        self.seat1.held_until = timezone.now() - timedelta(seconds=10)
        self.seat1.save()

        Reservation.objects.create(
            seat=self.seat1,
            transaction_id=self.tx_id,
            user_id=self.user_id,
            user_email=self.user_email,
            status=Reservation.Status.RESERVED,
        )

        confirm_payload = {
            "seat_ids": [self.seat1.id],
            "transaction_id": str(self.tx_id),
        }
        resp = self.client.post("/seats/confirm/", confirm_payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("code"), "HOLD_EXPIRED")

    def test_confirm_seat_idempotent_replay(self):
        """Confirming seats already BOOKED for this transaction returns 200 OK idempotently."""
        self.seat1.status = Seat.Status.BOOKED
        self.seat1.held_by_transaction_id = self.tx_id
        self.seat1.save()

        Reservation.objects.create(
            seat=self.seat1,
            transaction_id=self.tx_id,
            user_id=self.user_id,
            user_email=self.user_email,
            status=Reservation.Status.BOOKED,
        )

        confirm_payload = {
            "seat_ids": [self.seat1.id],
            "transaction_id": str(self.tx_id),
        }
        resp = self.client.post("/seats/confirm/", confirm_payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("confirmed", resp.data.get("message"))

    def test_release_seat_internal_compensation_releases_to_available(self):
        """Internal service key releases holds or bookings and records COORDINATOR_COMPENSATED reason."""
        self.seat1.status = Seat.Status.HELD
        self.seat1.held_by_transaction_id = self.tx_id
        self.seat1.held_until = timezone.now() + timedelta(minutes=5)
        self.seat1.save()

        Reservation.objects.create(
            seat=self.seat1,
            transaction_id=self.tx_id,
            user_id=self.user_id,
            user_email=self.user_email,
            status=Reservation.Status.RESERVED,
        )

        resp = self.client.post(
            "/seats/release/",
            {"transaction_id": str(self.tx_id)},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("released_count"), 1)

        self.seat1.refresh_from_db()
        self.assertEqual(self.seat1.status, Seat.Status.AVAILABLE)
        self.assertIsNone(self.seat1.held_by_transaction_id)
        self.assertIsNone(self.seat1.held_until)

        res = Reservation.objects.get(transaction_id=self.tx_id, seat=self.seat1)
        self.assertEqual(res.status, Reservation.Status.RELEASED)
        self.assertEqual(res.released_reason, "COORDINATOR_COMPENSATED")

    def test_release_seat_ownership_and_booked_protection_for_users(self):
        """Regular users cannot release other users' holds (403) or release BOOKED tickets (403)."""
        self.seat1.status = Seat.Status.HELD
        self.seat1.held_by_transaction_id = self.tx_id
        self.seat1.user_id = "usr_original_owner"
        self.seat1.save()

        Reservation.objects.create(
            seat=self.seat1,
            transaction_id=self.tx_id,
            user_id="usr_original_owner",
            user_email="orig@gmail.com",
            status=Reservation.Status.RESERVED,
        )

        with self.settings(IS_TESTING=False):
            # 1. Ownership mismatch
            with patch("inventory.views._get_authenticated_user_id", return_value="usr_different_user"):
                resp = self.client.post(
                    "/seats/release/",
                    {"transaction_id": str(self.tx_id)},
                    format="json",
                )
                self.assertEqual(resp.status_code, 403)
                self.assertEqual(resp.data.get("code"), "HOLD_OWNERSHIP_MISMATCH")

            # 2. Cannot release booked ticket
            res = Reservation.objects.get(transaction_id=self.tx_id)
            res.status = Reservation.Status.BOOKED
            res.save()
            with patch("inventory.views._get_authenticated_user_id", return_value="usr_original_owner"):
                resp = self.client.post(
                    "/seats/release/",
                    {"transaction_id": str(self.tx_id)},
                    format="json",
                )
                self.assertEqual(resp.status_code, 403)
                self.assertEqual(resp.data.get("code"), "CANNOT_RELEASE_BOOKED")

    def test_check_reservation_ground_truth_status(self):
        """check_reservation endpoint returns ground-truth status: BOOKED, RELEASED, or RESERVED."""
        url = f"/seats/check/?transaction_id={self.tx_id}"

        # 1. When non-existent
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), "NOT_RESERVED")

        # 2. When RESERVED
        res = Reservation.objects.create(
            seat=self.seat1,
            transaction_id=self.tx_id,
            user_id=self.user_id,
            user_email=self.user_email,
            status=Reservation.Status.RESERVED,
        )
        self.seat1.status = Seat.Status.HELD
        self.seat1.held_by_transaction_id = self.tx_id
        self.seat1.held_until = timezone.now() + timedelta(minutes=5)
        self.seat1.save()

        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), "INVENTORY_RESERVED")

        # 3. When BOOKED
        res.status = Reservation.Status.BOOKED
        res.save()
        self.seat1.status = Seat.Status.BOOKED
        self.seat1.save()

        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), "BOOKED")
        self.assertEqual(len(resp.data.get("seats")), 1)
        self.assertEqual(resp.data["seats"][0]["seat_label"], "A1")


class InventoryThrottlingUnitTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def test_anon_throttle_uses_session_id_when_provided(self):
        """Anonymous throttle creates cache key using session ID if provided in header."""
        request = self.factory.post(
            "/seats/release/",
            {"transaction_id": "tx_123"},
            HTTP_X_SESSION_ID="sess_user_alpha",
        )
        throttle = AnonIpRateThrottle()
        key = throttle.get_cache_key(request, None)
        self.assertIn("session_sess_user_alpha", key)

    def test_anon_throttle_falls_back_to_ip_when_no_session(self):
        """Anonymous throttle falls back to IP address when no session is present."""
        request = self.factory.post(
            "/seats/release/",
            {"transaction_id": "tx_123"},
            REMOTE_ADDR="192.168.1.100",
        )
        throttle = AnonIpRateThrottle()
        key = throttle.get_cache_key(request, None)
        self.assertIn("ip_", key)
        self.assertIn("192.168.1.100", key)

    def test_anon_throttle_bypasses_authenticated_jwt_users(self):
        """Anon throttle returns None for authenticated users so UserActionRateThrottle handles them."""
        request = self.factory.post("/seats/release/", {"transaction_id": "tx_123"})
        throttle = AnonIpRateThrottle()
        with patch("inventory.throttling._get_authenticated_user_id", return_value="usr_verified"):
            key = throttle.get_cache_key(request, None)
            self.assertIsNone(key)

    def test_check_reservation_throttle_uses_session_with_ip_fallback(self):
        """CheckReservationRateThrottle uses session ID if present, else client IP."""
        throttle = CheckReservationRateThrottle()

        # With session ID
        req_with_session = self.factory.get("/seats/check/", HTTP_X_SESSION_ID="sess_chk_789")
        key_session = throttle.get_cache_key(req_with_session, None)
        self.assertIn("session_sess_chk_789", key_session)

        # Without session ID (IP fallback)
        req_without_session = self.factory.get("/seats/check/", REMOTE_ADDR="10.0.0.5")
        key_ip = throttle.get_cache_key(req_without_session, None)
        self.assertIn("ip_", key_ip)
        self.assertIn("10.0.0.5", key_ip)

    def test_user_action_throttle_ignores_untrusted_client_payload(self):
        """UserActionRateThrottle rejects unverified request.data user_id and only trusts JWT."""
        throttle = UserActionRateThrottle()
        req_unverified = self.factory.post(
            "/seats/reserve/",
            {"user_id": "unverified_attacker"},
            format="json",
        )
        # Without valid JWT token
        key = throttle.get_cache_key(req_unverified, None)
        self.assertIsNone(key)

        # With valid JWT token
        with patch("inventory.throttling._get_authenticated_user_id", return_value="usr_jwt_999"):
            key = throttle.get_cache_key(req_unverified, None)
            self.assertIn("usr_jwt_999", key)

