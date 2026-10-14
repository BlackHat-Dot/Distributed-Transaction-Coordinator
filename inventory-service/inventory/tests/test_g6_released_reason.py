import uuid
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIClient, APIRequestFactory

from inventory.models import Event, Seat, Reservation
from inventory.services import reclaim_expired_holds
from inventory.views import reset_seats, release_seat, check_reservation


class TestG6ReleasedReason(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.factory = APIRequestFactory()
        self.event = Event.objects.create(name="G6 Venue Reset Tour")

    def test_g6_reset_seats_sets_admin_reset_reason_on_booked_reservations(self):
        """
        G6: reset_seats marks previously BOOKED reservations as RELEASED with released_reason='ADMIN_RESET'.
        """
        tid = uuid.uuid4()
        seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="1",
            price=Decimal("100.00"),
            status=Seat.Status.BOOKED,
            held_by_transaction_id=tid,
        )
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.BOOKED,
        )

        req = self.factory.post("/seats/reset/", HTTP_X_STAFF_KEY="dtc-staff-secret-key-2026")
        resp = reset_seats(req)
        self.assertEqual(resp.status_code, 200)

        res.refresh_from_db()
        self.assertEqual(res.status, Reservation.Status.RELEASED)
        self.assertEqual(res.released_reason, "ADMIN_RESET")

        # check_reservation returns released_reason
        check_req = self.factory.get(f"/seats/check/?transaction_id={tid}")
        check_resp = check_reservation(check_req)
        self.assertEqual(check_resp.status_code, 200)
        self.assertEqual(check_resp.data.get("status"), Reservation.Status.RELEASED)
        self.assertEqual(check_resp.data.get("released_reason"), "ADMIN_RESET")

    def test_g6_reclaim_expired_holds_sets_expired_hold_reason(self):
        """
        G6: reclaim_expired_holds sets released_reason='EXPIRED_HOLD'.
        """
        tid = uuid.uuid4()
        now = timezone.now()
        seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="2",
            price=Decimal("100.00"),
            status=Seat.Status.HELD,
            held_until=now - timedelta(seconds=10),
            held_by_transaction_id=tid,
        )
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
        )

        reclaimed, released = reclaim_expired_holds()
        self.assertEqual(reclaimed, 1)
        self.assertEqual(released, 1)

        res.refresh_from_db()
        self.assertEqual(res.status, Reservation.Status.RELEASED)
        self.assertEqual(res.released_reason, "EXPIRED_HOLD")

    def test_g6_release_seat_sets_appropriate_reason(self):
        """
        G6: release_seat sets 'COORDINATOR_COMPENSATED' when called with internal service key.
        """
        tid = uuid.uuid4()
        now = timezone.now()
        seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="3",
            price=Decimal("100.00"),
            status=Seat.Status.HELD,
            held_until=now + timedelta(minutes=5),
            held_by_transaction_id=tid,
        )
        res = Reservation.objects.create(
            seat=seat,
            transaction_id=tid,
            status=Reservation.Status.RESERVED,
        )

        req = self.factory.post(
            "/seats/release/",
            {"transaction_id": str(tid)},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY="dtc-internal-coordinator-secret-key-2026",
        )
        resp = release_seat(req)
        self.assertEqual(resp.status_code, 200)

        res.refresh_from_db()
        self.assertEqual(res.status, Reservation.Status.RELEASED)
        self.assertEqual(res.released_reason, "COORDINATOR_COMPENSATED")
