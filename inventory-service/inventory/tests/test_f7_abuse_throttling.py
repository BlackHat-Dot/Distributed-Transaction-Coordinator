import uuid
from decimal import Decimal
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from inventory.models import Event, Seat


class TestF7AbuseLimits(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.event = Event.objects.create(name="F7 Tour")
        self.seat = Seat.objects.create(
            event=self.event,
            row="A",
            seat_number="1",
            price=Decimal("100.00"),
            status=Seat.Status.AVAILABLE,
        )

    def tearDown(self):
        cache.clear()

    @override_settings(
        REST_FRAMEWORK={
            "DEFAULT_THROTTLE_RATES": {
                "inventory_user": "2/min",
                "inventory_anon": "1000/min",
            },
            "EXCEPTION_HANDLER": "inventory.throttling.inventory_exception_handler",
        }
    )
    def test_user_throttle_on_reserve_seat_returns_429(self):
        """
        F7: reserve_seat throttles per user_id and returns 429 with code 'THROTTLED'.
        """
        user_id = "f7_abuser"
        for i in range(2):
            resp = self.client.post(
                "/seats/reserve/",
                {
                    "seat_ids": [self.seat.id],
                    "transaction_id": str(uuid.uuid4()),
                    "user_id": user_id,
                },
                format="json",
            )
            # Allowed (first may be 200, second 409 because seat held, but NOT 429)
            self.assertNotEqual(resp.status_code, 429, f"Request {i+1} must not be throttled")

        # Third request exceeds 2/min limit
        resp3 = self.client.post(
            "/seats/reserve/",
            {
                "seat_ids": [self.seat.id],
                "transaction_id": str(uuid.uuid4()),
                "user_id": user_id,
            },
            format="json",
        )
        self.assertEqual(resp3.status_code, 429)
        self.assertEqual(resp3.data.get("code"), "THROTTLED")
        self.assertIn("error", resp3.data)

    @override_settings(
        REST_FRAMEWORK={
            "DEFAULT_THROTTLE_RATES": {
                "inventory_user": "2/min",
                "inventory_anon": "1000/min",
            },
            "EXCEPTION_HANDLER": "inventory.throttling.inventory_exception_handler",
        },
        INTERNAL_SERVICE_KEY="secret-internal-key",
    )
    def test_internal_service_key_exempt_from_user_throttle(self):
        """
        F7: Internal service calls presenting X-Internal-Service-Key are exempt from rate throttling.
        """
        user_id = "f7_coordinator_user"
        for i in range(5):
            resp = self.client.post(
                "/seats/reserve/",
                {
                    "seat_ids": [self.seat.id],
                    "transaction_id": str(uuid.uuid4()),
                    "user_id": user_id,
                },
                format="json",
                HTTP_X_INTERNAL_SERVICE_KEY="secret-internal-key",
            )
            self.assertNotEqual(resp.status_code, 429, "Internal coordinator calls must not be throttled")

    @override_settings(
        REST_FRAMEWORK={
            "DEFAULT_THROTTLE_RATES": {
                "inventory_user": "1000/min",
                "inventory_anon": "2/min",
            },
            "EXCEPTION_HANDLER": "inventory.throttling.inventory_exception_handler",
        }
    )
    def test_ip_throttle_on_seat_list_returns_429(self):
        """
        F7: seat_list throttles per client IP and returns 429 with code 'THROTTLED'.
        """
        for i in range(2):
            resp = self.client.get("/seats/")
            self.assertEqual(resp.status_code, 200)

        # 3rd request from same client IP exceeds 2/min limit
        resp3 = self.client.get("/seats/")
        self.assertEqual(resp3.status_code, 429)
        self.assertEqual(resp3.data.get("code"), "THROTTLED")
        self.assertIn("error", resp3.data)
