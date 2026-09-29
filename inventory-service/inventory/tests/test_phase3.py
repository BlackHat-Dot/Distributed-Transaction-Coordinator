import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from inventory.models import Event, Seat, Reservation
from inventory.serializers import PublicSeatSerializer, InternalSeatSerializer
from inventory.views import (
    _get_authenticated_user,
    _verify_internal_service_key,
    reset_seats,
    sweep_expired_holds,
    reserve_seat,
    release_seat,
    seat_list,
    seat_pricing,
)


def _generate_jwt(user_id, user_email="fan@example.com", expires_in=1800, secret=None, alg="HS256"):
    secret_bytes = (secret or settings.SECRET_KEY).encode("utf-8")
    header = {"alg": alg, "typ": "JWT"}
    now = int(time.time())
    payload = {
        "user_id": user_id,
        "user_email": user_email,
        "iat": now,
        "exp": now + expires_in,
        "sid": "test_session_123",
    }

    def b64url(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("utf-8")

    h_str = b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_str = b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig_input = f"{h_str}.{p_str}".encode("utf-8")
    sig = hmac.new(secret_bytes, sig_input, hashlib.sha256).digest()
    sig_str = b64url(sig)
    return f"{h_str}.{p_str}.{sig_str}"


class Phase3AuthAndSecurityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.factory = APIRequestFactory()
        self.event = Event.objects.create(
            name="Phase 3 Concert",
            venue="Phase 3 Arena",
            starts_at=timezone.now() + timedelta(days=5),
        )
        self.internal_key = getattr(settings, "INTERNAL_SERVICE_KEY", "dtc-internal-coordinator-secret-key-2026")

    # =========================================================================
    # FINDING 1: Internal Service Key Verification Hardening
    # =========================================================================
    def test_verify_internal_service_key_rejects_invalid_key(self):
        req = self.factory.post("/test/", HTTP_X_INTERNAL_SERVICE_KEY="invalid-secret-key")
        err, is_internal = _verify_internal_service_key(req)
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 403)
        self.assertFalse(is_internal)

    def test_verify_internal_service_key_accepts_valid_key(self):
        req = self.factory.post("/test/", HTTP_X_INTERNAL_SERVICE_KEY=self.internal_key)
        err, is_internal = _verify_internal_service_key(req)
        self.assertIsNone(err)
        self.assertTrue(is_internal)

    @patch("inventory.views.settings.IS_TESTING", False)
    def test_verify_internal_service_key_rejects_missing_key_in_production(self):
        req = self.factory.post("/test/")
        err, is_internal = _verify_internal_service_key(req)
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 403)
        self.assertFalse(is_internal)

    # =========================================================================
    # FINDING 2: Cryptographic JWT Authentication & Identity Header Spoofing
    # =========================================================================
    def test_jwt_valid_signature_extracts_identity(self):
        token = _generate_jwt("usr_valid_42", "valid@example.com")
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        user = _get_authenticated_user(req)
        self.assertIsNotNone(user)
        self.assertEqual(user["user_id"], "usr_valid_42")
        self.assertEqual(user["user_email"], "valid@example.com")
        self.assertEqual(user["sid"], "test_session_123")

    def test_jwt_expired_token_rejected(self):
        token = _generate_jwt("usr_expired", "expired@example.com", expires_in=-10)
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        user = _get_authenticated_user(req)
        self.assertIsNone(user)

    def test_jwt_tampered_signature_rejected(self):
        token = _generate_jwt("usr_victim", "victim@example.com", secret="attacker-wrong-secret")
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        user = _get_authenticated_user(req)
        self.assertIsNone(user)

    def test_spoofed_identity_headers_ignored_when_unverified_headers_disallowed(self):
        req = self.factory.get(
            "/test/",
            HTTP_X_USER_ID="usr_spoofed_evil",
            HTTP_X_USER_EMAIL="spoofed@evil.com",
        )
        with patch("inventory.views.settings.ALLOW_UNVERIFIED_IDENTITY_HEADERS", False):
            user = _get_authenticated_user(req)
            self.assertIsNone(user)

    # =========================================================================
    # FINDING 3: sweep_expired_holds Authentication
    # =========================================================================
    @patch("inventory.views.settings.IS_TESTING", False)
    def test_sweep_expired_holds_endpoint_requires_auth_in_production(self):
        req = self.factory.post("/seats/sweep-expired/")
        resp = sweep_expired_holds(req)
        self.assertEqual(resp.status_code, 403)

    @patch("inventory.views.settings.IS_TESTING", False)
    def test_sweep_expired_holds_endpoint_allows_internal_key(self):
        req = self.factory.post("/seats/sweep-expired/", HTTP_X_INTERNAL_SERVICE_KEY=self.internal_key)
        resp = sweep_expired_holds(req)
        self.assertEqual(resp.status_code, 200)

    @override_settings(STAFF_API_KEY="dtc-staff-secret-key-2026")
    @patch("inventory.views.settings.IS_TESTING", False)
    def test_sweep_expired_holds_endpoint_allows_staff_key(self):
        req = self.factory.post("/seats/sweep-expired/", HTTP_X_STAFF_KEY="dtc-staff-secret-key-2026")
        resp = sweep_expired_holds(req)
        self.assertEqual(resp.status_code, 200)

    # =========================================================================
    # FINDING 8: release_seat Ownership Enforcement
    # =========================================================================
    def test_release_seat_by_different_user_forbidden(self):
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="B",
            seat_number="1",
            price=150.00,
            status=Seat.Status.HELD,
            held_until=timezone.now() + timedelta(minutes=10),
            held_by_transaction_id=tid,
            user_id="usr_original_owner",
            user_email="owner@example.com",
        )
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            user_id="usr_original_owner",
            user_email="owner@example.com",
            status=Reservation.Status.RESERVED,
        )

        attacker_token = _generate_jwt("usr_attacker", "attacker@example.com")
        req = self.factory.post(
            "/seats/release/",
            {"transaction_id": str(tid)},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {attacker_token}",
        )
        resp = release_seat(req)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data.get("code"), "HOLD_OWNERSHIP_MISMATCH")

        seat.refresh_from_db()
        self.assertEqual(seat.status, Seat.Status.HELD)

    def test_release_seat_by_owner_succeeds(self):
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="B",
            seat_number="2",
            price=150.00,
            status=Seat.Status.HELD,
            held_until=timezone.now() + timedelta(minutes=10),
            held_by_transaction_id=tid,
            user_id="usr_owner_789",
            user_email="owner789@example.com",
        )
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            user_id="usr_owner_789",
            user_email="owner789@example.com",
            status=Reservation.Status.RESERVED,
        )

        owner_token = _generate_jwt("usr_owner_789", "owner789@example.com")
        req = self.factory.post(
            "/seats/release/",
            {"transaction_id": str(tid)},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {owner_token}",
        )
        resp = release_seat(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("released_count"), 1)

        seat.refresh_from_db()
        self.assertEqual(seat.status, Seat.Status.AVAILABLE)

    # =========================================================================
    # FINDING 16: Public vs Internal Seat Serializer Info Leakage & is_mine
    # =========================================================================
    def test_public_seat_serializer_does_not_leak_internal_fields(self):
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="C",
            seat_number="1",
            price=120.00,
            status=Seat.Status.HELD,
            held_until=timezone.now() + timedelta(minutes=10),
            held_by_transaction_id=tid,
            user_id="usr_secret_123",
            user_email="secret@example.com",
        )
        serializer = PublicSeatSerializer(seat)
        data = serializer.data

        # Must not contain private user or transaction tracking
        self.assertNotIn("held_by_transaction_id", data)
        self.assertNotIn("user_id", data)
        self.assertNotIn("user_email", data)
        self.assertIn("id", data)
        self.assertIn("status", data)
        self.assertIn("is_mine", data)
        self.assertFalse(data["is_mine"])

    def test_public_seat_serializer_computes_is_mine_true_for_owner(self):
        tid = uuid.uuid4()
        user_id = "usr_ticket_holder"
        seat = Seat.objects.create(
            event=self.event,
            row="C",
            seat_number="2",
            price=120.00,
            status=Seat.Status.HELD,
            held_until=timezone.now() + timedelta(minutes=10),
            held_by_transaction_id=tid,
            user_id=user_id,
            user_email="holder@example.com",
        )

        token = _generate_jwt(user_id, "holder@example.com")
        req = self.factory.get("/seats/", HTTP_AUTHORIZATION=f"Bearer {token}")
        serializer = PublicSeatSerializer(seat, context={"request": req})
        self.assertTrue(serializer.data["is_mine"])

    def test_public_seat_serializer_computes_is_mine_false_for_non_owner(self):
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="C",
            seat_number="3",
            price=120.00,
            status=Seat.Status.HELD,
            held_until=timezone.now() + timedelta(minutes=10),
            held_by_transaction_id=tid,
            user_id="usr_user_a",
            user_email="user_a@example.com",
        )

        token_b = _generate_jwt("usr_user_b", "user_b@example.com")
        req = self.factory.get("/seats/", HTTP_AUTHORIZATION=f"Bearer {token_b}")
        serializer = PublicSeatSerializer(seat, context={"request": req})
        self.assertFalse(serializer.data["is_mine"])

    def test_internal_seat_serializer_includes_internal_fields(self):
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="C",
            seat_number="4",
            price=120.00,
            status=Seat.Status.HELD,
            held_until=timezone.now() + timedelta(minutes=10),
            held_by_transaction_id=tid,
            user_id="usr_internal_check",
            user_email="internal@example.com",
        )
        serializer = InternalSeatSerializer(seat)
        data = serializer.data

        self.assertEqual(data["held_by_transaction_id"], str(tid))
        self.assertEqual(data["user_id"], "usr_internal_check")
        self.assertEqual(data["user_email"], "internal@example.com")

    # =========================================================================
    # reset_seats Protection
    # =========================================================================
    @patch("inventory.views.settings.ALLOW_UNAUTHENTICATED_RESET", False)
    def test_reset_seats_requires_staff_key_when_unauthenticated_reset_disabled(self):
        req = self.factory.post("/seats/reset/")
        resp = reset_seats(req)
        self.assertEqual(resp.status_code, 403)

    @patch("inventory.views.settings.ALLOW_UNAUTHENTICATED_RESET", False)
    def test_reset_seats_allows_staff_key_when_unauthenticated_reset_disabled(self):
        req = self.factory.post("/seats/reset/", HTTP_X_STAFF_KEY="dtc-staff-secret-key-2026")
        resp = reset_seats(req)
        self.assertEqual(resp.status_code, 200)
