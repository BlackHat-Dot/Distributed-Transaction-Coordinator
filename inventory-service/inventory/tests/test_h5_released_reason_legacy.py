import uuid
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

import importlib
from inventory.models import Event, Seat, Reservation
from inventory.views import check_reservation, confirm_seat, reset_seats
from django.apps import apps

migration_0018 = importlib.import_module("inventory.migrations.0018_populate_released_reason_unknown_legacy")
populate_unknown_legacy = migration_0018.populate_unknown_legacy
reverse_func = migration_0018.reverse_func


class TestH5ReleasedReasonLegacy(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.event = Event.objects.create(name="H5 Legacy Test Event")

    def test_check_reservation_returns_unknown_legacy_for_empty_reason(self):
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="10",
            price=Decimal("50.00"),
            status=Seat.Status.AVAILABLE,
        )
        Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RELEASED,
            released_reason="",
        )

        req = self.factory.get(f"/reserve/check/?transaction_id={tid}")
        resp = check_reservation(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("status"), Reservation.Status.RELEASED)
        self.assertEqual(resp.data.get("released_reason"), "UNKNOWN_LEGACY")

    def test_confirm_seat_returns_unknown_legacy_for_legacy_released_reservation(self):
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="11",
            price=Decimal("50.00"),
            status=Seat.Status.AVAILABLE,
        )
        Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RELEASED,
            released_reason="",
        )

        req = self.factory.post(
            "/seats/confirm/",
            {"transaction_id": str(tid), "seat_ids": [seat.id]},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY="dtc-internal-coordinator-secret-key-2026",
        )
        resp = confirm_seat(req)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("status"), "HOLD_EXPIRED")
        self.assertEqual(resp.data.get("released_reason"), "UNKNOWN_LEGACY")

    def test_confirm_seat_returns_admin_reset_when_seat_was_reset(self):
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="12",
            price=Decimal("50.00"),
            status=Seat.Status.BOOKED,
            held_by_transaction_id=tid,
        )
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.BOOKED,
        )

        # Admin resets venue seats
        reset_req = self.factory.post("/seats/reset/", HTTP_X_STAFF_KEY="dtc-staff-secret-key-2026")
        reset_resp = reset_seats(reset_req)
        self.assertEqual(reset_resp.status_code, 200)

        res.refresh_from_db()
        self.assertEqual(res.status, Reservation.Status.RELEASED)
        self.assertEqual(res.released_reason, "ADMIN_RESET")

        # Now attempting to confirm returns 409 with released_reason='ADMIN_RESET'
        confirm_req = self.factory.post(
            "/seats/confirm/",
            {"transaction_id": str(tid), "seat_ids": [seat.id]},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY="dtc-internal-coordinator-secret-key-2026",
        )
        confirm_resp = confirm_seat(confirm_req)
        self.assertEqual(confirm_resp.status_code, 409)
        self.assertEqual(confirm_resp.data.get("released_reason"), "ADMIN_RESET")

    def test_migration_0018_populates_unknown_legacy(self):
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="13",
            price=Decimal("50.00"),
            status=Seat.Status.AVAILABLE,
        )
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RELEASED,
            released_reason="",
        )

        populate_unknown_legacy(apps, None)
        res.refresh_from_db()
        self.assertEqual(res.released_reason, "UNKNOWN_LEGACY")

        # Test reverse
        reverse_func(apps, None)
        res.refresh_from_db()
        self.assertEqual(res.released_reason, "")
