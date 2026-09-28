import uuid
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from inventory.models import Event, Seat, Reservation
from inventory.views import (
    _validate_reserve_input,
    reserve_seat,
    confirm_seat,
    release_seat,
    reset_seats,
)


class Phase4HygieneAndHardeningTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.factory = APIRequestFactory()
        self.event1 = Event.objects.create(
            name="Phase 4 Festival 1",
            venue="Arena 1",
            starts_at=timezone.now() + timedelta(days=2),
        )
        self.event2 = Event.objects.create(
            name="Phase 4 Festival 2",
            venue="Arena 2",
            starts_at=timezone.now() + timedelta(days=4),
        )

    # =========================================================================
    # FINDING 18: Input Validation - Dedupe & Max Cap 50
    # =========================================================================
    def test_validate_reserve_input_deduplicates_seat_ids(self):
        data = {"seat_ids": [10, 20, 10, 30, 20], "transaction_id": str(uuid.uuid4())}
        seat_ids, tid, err = _validate_reserve_input(data)
        self.assertIsNone(err)
        self.assertEqual(seat_ids, [10, 20, 30])

    def test_validate_reserve_input_caps_at_50_seats(self):
        data = {"seat_ids": list(range(1, 52)), "transaction_id": str(uuid.uuid4())}
        seat_ids, tid, err = _validate_reserve_input(data)
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 400)
        self.assertEqual(err.data.get("code"), "MAX_SEATS_EXCEEDED")

    # =========================================================================
    # FINDINGS 19 & 25: Decimal Model Default and Seeding
    # =========================================================================
    def test_seat_price_model_default_is_decimal(self):
        seat = Seat.objects.create(
            event=self.event1,
            row="Z",
            seat_number="99",
        )
        self.assertEqual(seat.price, Decimal("500.00"))
        self.assertIsInstance(seat.price, Decimal)

    # =========================================================================
    # FINDING 20: MAX_USER_HOLDS Default Fallback
    # =========================================================================
    def test_max_user_holds_fallback_to_10_when_unset(self):
        seat = Seat.objects.create(
            event=self.event1,
            row="A",
            seat_number="1",
            price=Decimal("100.00"),
        )
        tid = str(uuid.uuid4())
        req = self.factory.post(
            "/seats/reserve/",
            {"seat_ids": [seat.id], "transaction_id": tid, "user_id": "usr_default_quota"},
            format="json",
        )
        with patch.object(settings, "MAX_USER_HOLDS", None):
            resp = reserve_seat(req)
            self.assertEqual(resp.status_code, 200)

    # =========================================================================
    # FINDING 23: DatabaseError Masking / No Leakage
    # =========================================================================
    def test_database_error_masked_in_reserve_seat(self):
        tid = str(uuid.uuid4())
        req = self.factory.post(
            "/seats/reserve/",
            {"seat_ids": [1], "transaction_id": tid},
            format="json",
        )
        with patch("inventory.models.Seat.objects.select_for_update", side_effect=DatabaseError("FATAL: connection dead")):
            resp = reserve_seat(req)
            self.assertEqual(resp.status_code, 500)
            self.assertEqual(resp.data.get("error"), "Internal server error occurred.")
            self.assertEqual(resp.data.get("code"), "DATABASE_ERROR")
            # Must NOT leak Postgres / internal DB error details to client
            self.assertNotIn("FATAL", str(resp.data))

    def test_database_error_masked_in_confirm_seat(self):
        tid = str(uuid.uuid4())
        req = self.factory.post(
            "/seats/confirm/",
            {"seat_ids": [1], "transaction_id": tid},
            format="json",
        )
        with patch("inventory.models.Seat.objects.select_for_update", side_effect=DatabaseError("FATAL: disk corrupt")):
            resp = confirm_seat(req)
            self.assertEqual(resp.status_code, 500)
            self.assertEqual(resp.data.get("error"), "Internal server error occurred.")
            self.assertEqual(resp.data.get("code"), "DATABASE_ERROR")
            self.assertNotIn("FATAL", str(resp.data))

    def test_database_error_masked_in_release_seat(self):
        tid = str(uuid.uuid4())
        req = self.factory.post(
            "/seats/release/",
            {"transaction_id": tid},
            format="json",
        )
        with patch("inventory.models.Reservation.objects.filter", side_effect=DatabaseError("FATAL: timeout")):
            resp = release_seat(req)
            self.assertEqual(resp.status_code, 500)
            self.assertEqual(resp.data.get("error"), "Internal server error occurred.")
            self.assertEqual(resp.data.get("code"), "DATABASE_ERROR")
            self.assertNotIn("FATAL", str(resp.data))

    # =========================================================================
    # FINDING 27: reset_seats Scoped to event_id
    # =========================================================================
    def test_reset_seats_scoped_to_event_id(self):
        tid1 = uuid.uuid4()
        tid2 = uuid.uuid4()

        seat_e1 = Seat.objects.create(
            event=self.event1,
            row="A",
            seat_number="1",
            price=Decimal("100.00"),
            status=Seat.Status.HELD,
            held_until=timezone.now() + timedelta(minutes=10),
            held_by_transaction_id=tid1,
        )
        res_e1 = Reservation.objects.create(
            seat=seat_e1,
            transaction_id=tid1,
            status=Reservation.Status.RESERVED,
        )

        seat_e2 = Seat.objects.create(
            event=self.event2,
            row="B",
            seat_number="1",
            price=Decimal("100.00"),
            status=Seat.Status.HELD,
            held_until=timezone.now() + timedelta(minutes=10),
            held_by_transaction_id=tid2,
        )
        res_e2 = Reservation.objects.create(
            seat=seat_e2,
            transaction_id=tid2,
            status=Reservation.Status.RESERVED,
        )

        # Reset only event1
        req = self.factory.post("/seats/reset/", {"event_id": self.event1.id}, format="json")
        resp = reset_seats(req)
        self.assertEqual(resp.status_code, 200)

        # Event 1 seat must be reset
        seat_e1.refresh_from_db()
        self.assertEqual(seat_e1.status, Seat.Status.AVAILABLE)
        self.assertFalse(Reservation.objects.filter(id=res_e1.id).exists())

        # Event 2 seat must remain untouched
        seat_e2.refresh_from_db()
        self.assertEqual(seat_e2.status, Seat.Status.HELD)
        self.assertTrue(Reservation.objects.filter(id=res_e2.id).exists())
