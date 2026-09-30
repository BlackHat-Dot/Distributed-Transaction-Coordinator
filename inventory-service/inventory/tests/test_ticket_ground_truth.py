import uuid
from decimal import Decimal
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import Event, Seat, Reservation


class TicketGroundTruthTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = Event.objects.create(
            name="Coldplay: Music of the Spheres World Tour",
            venue="Grand Arena Stadium",
        )
        self.owner_tid = uuid.uuid4()
        self.other_tid = uuid.uuid4()

        # Seat is currently held/owned by owner_tid
        self.seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="1",
            price=Decimal("750.00"),
            status=Seat.Status.BOOKED,
            held_by_transaction_id=self.owner_tid,
        )

        # Reservation record created for owner_tid
        self.res = Reservation.objects.create(
            seat=self.seat,
            transaction_id=self.owner_tid,
            status=Reservation.Status.BOOKED,
        )

    def test_owner_transaction_check_returns_booked(self):
        """Owner transaction querying /seats/check/ receives BOOKED with event/seat metadata."""
        resp = self.client.get(f"/seats/check/?transaction_id={self.owner_tid}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("status"), "BOOKED")
        self.assertEqual(data.get("transaction_id"), str(self.owner_tid))
        self.assertEqual(data.get("event", {}).get("name"), "Coldplay: Music of the Spheres World Tour")
        self.assertEqual(len(data.get("seats", [])), 1)
        self.assertEqual(data.get("seats")[0]["seat_label"], "A1")

    def test_non_owner_transaction_check_returns_released_seat_taken(self):
        """When seat.held_by_transaction_id belongs to another transaction, /seats/check/ returns RELEASED with SEAT_TAKEN_BY_ANOTHER_TRANSACTION."""
        # Seat was taken over / reallocated to another transaction
        self.seat.held_by_transaction_id = self.other_tid
        self.seat.save(update_fields=["held_by_transaction_id"])

        resp = self.client.get(f"/seats/check/?transaction_id={self.owner_tid}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn(data.get("status"), [Reservation.Status.RELEASED, "RELEASED"])
        self.assertEqual(data.get("transaction_id"), str(self.owner_tid))
        self.assertEqual(data.get("released_reason"), "SEAT_TAKEN_BY_ANOTHER_TRANSACTION")
