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
