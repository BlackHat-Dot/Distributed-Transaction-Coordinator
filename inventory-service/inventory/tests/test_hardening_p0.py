import time
import uuid
import jwt
from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory

from inventory.views import _get_authenticated_user


class TestA1JWTVerification(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.secret = "test-secret-key-123"

    @override_settings(SECRET_KEY="test-secret-key-123")
    def test_a1_valid_token_with_secret_key(self):
        now = int(time.time())
        token = jwt.encode(
            {"user_id": "usr_99", "user_email": "fan@example.com", "sid": "sid_123", "exp": now + 3600},
            self.secret,
            algorithm="HS256",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNotNone(user)
        self.assertEqual(user, {"user_id": "usr_99", "user_email": "fan@example.com", "sid": "sid_123"})

    @override_settings(SECRET_KEY="test-secret-key-123", JWT_SIGNING_KEY="custom-jwt-key-456")
    def test_a1_valid_token_with_jwt_signing_key(self):
        now = int(time.time())
        token = jwt.encode(
            {"user_id": "usr_custom", "user_email": "custom@example.com", "sid": "sid_custom", "exp": now + 3600},
            "custom-jwt-key-456",
            algorithm="HS256",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNotNone(user)
        self.assertEqual(user, {"user_id": "usr_custom", "user_email": "custom@example.com", "sid": "sid_custom"})

    @override_settings(SECRET_KEY="test-secret-key-123")
    def test_a1_missing_exp_rejected(self):
        # Tokens without exp must be rejected
        token = jwt.encode(
            {"user_id": "usr_no_exp", "user_email": "noexp@example.com", "sid": "sid_no_exp"},
            self.secret,
            algorithm="HS256",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNone(user, "Tokens without exp claim must be rejected")

    @override_settings(SECRET_KEY="test-secret-key-123")
    def test_a1_expired_token_rejected(self):
        now = int(time.time())
        token = jwt.encode(
            {"user_id": "usr_exp", "user_email": "exp@example.com", "exp": now - 100},
            self.secret,
            algorithm="HS256",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNone(user)

    @override_settings(SECRET_KEY="test-secret-key-123")
    def test_a1_wrong_alg_none_rejected(self):
        # Algorithm "none" or without signature must be rejected
        now = int(time.time())
        token = jwt.encode(
            {"user_id": "usr_none", "user_email": "none@example.com", "exp": now + 3600},
            key="",
            algorithm="none",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNone(user)

    @override_settings(SECRET_KEY="test-secret-key-123")
    def test_a1_wrong_alg_hs512_rejected(self):
        now = int(time.time())
        token = jwt.encode(
            {"user_id": "usr_hs512", "user_email": "hs512@example.com", "exp": now + 3600},
            self.secret,
            algorithm="HS512",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNone(user)

    @override_settings(SECRET_KEY="test-secret-key-123")
    def test_a1_bad_signature_rejected(self):
        now = int(time.time())
        token = jwt.encode(
            {"user_id": "usr_bad", "user_email": "bad@example.com", "exp": now + 3600},
            "wrong-secret-key",
            algorithm="HS256",
        )
        req = self.factory.get("/test/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with override_settings(ALLOW_UNVERIFIED_IDENTITY_HEADERS=False):
            user = _get_authenticated_user(req)
        self.assertIsNone(user)


class TestA2StaffAndInternalKeySettings(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    @override_settings(STAFF_API_KEY="dynamic-staff-key-888")
    def test_a2_staff_key_read_from_settings_at_call_time(self):
        from inventory.views import _is_staff_request
        req = self.factory.get("/test/", HTTP_X_STAFF_KEY="dynamic-staff-key-888")
        self.assertTrue(_is_staff_request(req))

    @override_settings(STAFF_API_KEY="", IS_TESTING=True, DEBUG=True)
    def test_a2_no_hardcoded_staff_key_fallback(self):
        # Even with DEBUG=True and IS_TESTING=True, the fallback secret 'dtc-staff-secret-key-2026' must NOT work
        from inventory.views import _is_staff_request
        req = self.factory.get("/test/", HTTP_X_STAFF_KEY="dtc-staff-secret-key-2026")
        self.assertFalse(_is_staff_request(req), "Hardcoded fallback secret must be removed")

    @override_settings(INTERNAL_SERVICE_KEY="dynamic-internal-key-999", IS_TESTING=False)
    def test_a2_internal_key_read_from_settings_at_call_time(self):
        from inventory.views import _verify_internal_service_key
        req = self.factory.get("/test/", HTTP_X_INTERNAL_SERVICE_KEY="dynamic-internal-key-999")
        err, is_internal = _verify_internal_service_key(req)
        self.assertIsNone(err)
        self.assertTrue(is_internal)


class TestA3BypassFlagsAndResetSeats(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def test_a3_system_check_detects_unsafe_flags_when_debug_false(self):
        import sys
        from unittest import mock
        from inventory.checks import check_bypass_flags
        with mock.patch.object(sys, "argv", ["manage.py", "check"]):
            with override_settings(DEBUG=False, IS_TESTING=True):
                errors = check_bypass_flags(None)
                self.assertTrue(any("IS_TESTING" in e.msg for e in errors))

            with override_settings(DEBUG=False, IS_TESTING=False, ALLOW_UNVERIFIED_IDENTITY_HEADERS=True):
                errors = check_bypass_flags(None)
                self.assertTrue(any("ALLOW_UNVERIFIED_IDENTITY_HEADERS" in e.msg for e in errors))

            with override_settings(DEBUG=False, IS_TESTING=False, ALLOW_UNAUTHENTICATED_RESET=True):
                errors = check_bypass_flags(None)
                self.assertTrue(any("ALLOW_UNAUTHENTICATED_RESET" in e.msg for e in errors))

            with override_settings(DEBUG=False, IS_TESTING=False, ALLOW_UNVERIFIED_IDENTITY_HEADERS=False, ALLOW_UNAUTHENTICATED_RESET=False):
                errors = check_bypass_flags(None)
                self.assertEqual(errors, [])

    def test_f2_app_config_ready_raises_improperly_configured_when_debug_false_and_bypass_flags(self):
        import sys
        from unittest import mock
        from django.apps import apps
        from django.core.exceptions import ImproperlyConfigured
        app_config = apps.get_app_config("inventory")

        for server_argv in (["manage.py", "runserver"], ["gunicorn", "inventory_service.wsgi:application"]):
            with mock.patch.object(sys, "argv", server_argv):
                for flag in ("IS_TESTING", "ALLOW_UNVERIFIED_IDENTITY_HEADERS", "ALLOW_UNAUTHENTICATED_RESET"):
                    with self.subTest(server=server_argv[0], flag=flag):
                        kwargs = {"DEBUG": False, "IS_TESTING": False, "ALLOW_UNVERIFIED_IDENTITY_HEADERS": False, "ALLOW_UNAUTHENTICATED_RESET": False}
                        kwargs[flag] = True
                        with override_settings(**kwargs):
                            with self.assertRaises(ImproperlyConfigured):
                                app_config.ready()

    def test_f2_security_check_runs_on_normal_check_without_deploy(self):
        import sys
        from unittest import mock
        from django.core.checks import run_checks, Tags
        with mock.patch.object(sys, "argv", ["manage.py", "check"]):
            with override_settings(DEBUG=False, IS_TESTING=True):
                errors = run_checks(tags=[Tags.security])
                self.assertTrue(any("IS_TESTING" in e.msg for e in errors), "Tags.security check must run on standard check without deploy=True")

    @override_settings(DEBUG=False, ALLOW_UNAUTHENTICATED_RESET=True, STAFF_API_KEY="staff-key-123")
    def test_a3_reset_seats_in_non_debug_requires_staff_key(self):
        from inventory.views import reset_seats
        # In non-DEBUG, staff key is required even if ALLOW_UNAUTHENTICATED_RESET is True
        req = self.factory.post("/seats/reset/")
        resp = reset_seats(req)
        self.assertEqual(resp.status_code, 403)

    @override_settings(DEBUG=False, STAFF_API_KEY="staff-key-123")
    def test_a3_reset_seats_logs_audit_trail(self):
        import logging
        from inventory.views import reset_seats
        from inventory.models import Event, Seat, Reservation
        event = Event.objects.create(name="Audit Test Event")
        seat = Seat.objects.create(event=event, row="Z", seat_number="1", status=Seat.Status.BOOKED)
        Reservation.objects.create(seat=seat, transaction_id=uuid.uuid4(), status=Reservation.Status.BOOKED)

        req = self.factory.post("/seats/reset/", HTTP_X_STAFF_KEY="staff-key-123")
        with self.assertLogs("inventory", level="INFO") as log_cm:
            resp = reset_seats(req)
        self.assertEqual(resp.status_code, 200)
        audit_logs = [m for m in log_cm.output if "AUDIT" in m and "reset_seats" in m]
        self.assertTrue(len(audit_logs) > 0, "Audit log must be emitted on reset_seats")
        log_line = audit_logs[0]
        self.assertIn("who", log_line)
        self.assertIn("rows_affected", log_line)
        self.assertIn("before_counts", log_line)

    @override_settings(DEBUG=False, STAFF_API_KEY="staff-key-123")
    def test_f5_reset_seats_does_not_delete_booked_reservations(self):
        """
        F5: reset_seats must not hard-delete BOOKED reservations;
        it should mark them RELEASED to preserve the audit trail.
        """
        from inventory.views import reset_seats
        from inventory.models import Event, Seat, Reservation
        event = Event.objects.create(name="F5 Event")
        seat = Seat.objects.create(event=event, row="F", seat_number="5", status=Seat.Status.BOOKED)
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=uuid.uuid4(),
            status=Reservation.Status.BOOKED,
            user_id="customer_f5",
        )

        req = self.factory.post("/seats/reset/", HTTP_X_STAFF_KEY="staff-key-123")
        resp = reset_seats(req)
        self.assertEqual(resp.status_code, 200)

        # Seat is reset to AVAILABLE
        seat.refresh_from_db()
        self.assertEqual(seat.status, Seat.Status.AVAILABLE)

        # Reservation is NOT hard-deleted; it is marked RELEASED
        self.assertTrue(Reservation.objects.filter(id=res.id).exists(), "BOOKED reservation must not be deleted")
        res.refresh_from_db()
        self.assertEqual(res.status, Reservation.Status.RELEASED)


class TestA4PresenceCheckoutProtection(TestCase):
    def test_a4_presence_reclaim_skipped_when_checkout_key_exists(self):
        from unittest.mock import MagicMock, patch
        from datetime import timedelta
        from django.utils import timezone
        from inventory.models import Event, Seat, Reservation
        from inventory.views import reclaim_expired_holds

        event = Event.objects.create(name="Checkout Test Event")
        tid = uuid.uuid4()
        now = timezone.now()
        # Seat hold started 3 minutes ago (hold age = 180s >= 120s grace period), held_until is 7 minutes in future
        held_until = now + timedelta(minutes=7)
        seat = Seat.objects.create(
            event=event,
            row="A",
            seat_number="1",
            status=Seat.Status.HELD,
            held_until=held_until,
            held_by_transaction_id=tid,
            user_id="usr_checkout",
            user_email="shopper@test.com",
        )
        Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
            user_id="usr_checkout",
            user_email="shopper@test.com",
        )

        # Mock Redis client where dtc:session:shopper@test.com is 0 (lapsed),
        # but dtc:checkout:{tid} is 1 (mid-payment checkout active!)
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        # Pipeline execution returns exists responses
        def pipe_execute():
            # Return list of results for each pipeline command in order
            # The current un-fixed code only pipelines unique_emails: [0] -> expired!
            # Our fixed code will pipeline unique_emails + unique_tids: [0, 1]
            calls = mock_pipe.exists.call_args_list
            res = []
            for call in calls:
                key = call[0][0]
                if key.startswith("dtc:checkout:"):
                    res.append(1)  # Checkout key EXISTS
                else:
                    res.append(0)  # Session key does NOT exist
            return res

        mock_pipe.execute.side_effect = pipe_execute

        with patch("inventory.services.get_redis_client", return_value=mock_redis):
            reclaimed, released = reclaim_expired_holds()

        self.assertEqual(reclaimed, 0, "Seat must not be reclaimed when checkout key exists")
        self.assertEqual(released, 0)
        seat.refresh_from_db()
        self.assertEqual(seat.status, Seat.Status.HELD)



