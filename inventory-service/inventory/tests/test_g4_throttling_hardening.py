import uuid
from decimal import Decimal
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from inventory.models import Event, Seat


class TestG4ThrottlingHardening(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.event = Event.objects.create(name="G4 Tour")
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
            "NUM_PROXIES": 1,
            "DEFAULT_THROTTLE_RATES": {
                "inventory_reserve": "2/min",
            },
            "EXCEPTION_HANDLER": "inventory.throttling.inventory_exception_handler",
        },
        INTERNAL_SERVICE_KEY="real-internal-key-999",
        STAFF_API_KEY="real-staff-key-888",
    )
    def test_random_internal_and_staff_keys_are_not_exempt(self):
        """
        G4: Exemption requires valid key matching configured secrets.
        Random or invalid header values must NEVER be exempt from throttling.
        """
        user_id = "attacker_with_random_keys"

        # 1. Random X-Internal-Service-Key rejected or throttled
        for i in range(2):
            resp = self.client.post(
                "/seats/reserve/",
                {"seat_ids": [self.seat.id], "transaction_id": str(uuid.uuid4()), "user_id": user_id},
                format="json",
                HTTP_X_INTERNAL_SERVICE_KEY="fake-random-key",
            )
            # Fake key is rejected with 403 by auth or processed; either way after 2 requests it must throttle
            self.assertIn(resp.status_code, (403, 409, 200))

        # 3rd request with fake key
        resp3 = self.client.post(
            "/seats/reserve/",
            {"seat_ids": [self.seat.id], "transaction_id": str(uuid.uuid4()), "user_id": user_id},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY="fake-random-key",
        )
        self.assertEqual(resp3.status_code, 429, "Invalid internal key must not be exempt from throttling")

        # 2. Random X-Staff-Key rejected or throttled
        cache.clear()
        user_id2 = "attacker_with_fake_staff_key"
        for i in range(2):
            resp = self.client.post(
                "/seats/reserve/",
                {"seat_ids": [self.seat.id], "transaction_id": str(uuid.uuid4()), "user_id": user_id2},
                format="json",
                HTTP_X_STAFF_KEY="fake-staff-key",
            )
            self.assertNotEqual(resp.status_code, 429)

        resp3_staff = self.client.post(
            "/seats/reserve/",
            {"seat_ids": [self.seat.id], "transaction_id": str(uuid.uuid4()), "user_id": user_id2},
            format="json",
            HTTP_X_STAFF_KEY="fake-staff-key",
        )
        self.assertEqual(resp3_staff.status_code, 429, "Invalid staff key must not be exempt from throttling")

    @override_settings(
        REST_FRAMEWORK={
            "NUM_PROXIES": 1,
            "DEFAULT_THROTTLE_RATES": {
                "inventory_seat_list": "2/min",
            },
            "EXCEPTION_HANDLER": "inventory.throttling.inventory_exception_handler",
        }
    )
    def test_spoofed_x_forwarded_for_does_not_create_fresh_bucket(self):
        """
        G4: With NUM_PROXIES=1, the client IP is taken from the last proxy hop (203.0.113.195).
        An attacker spoofing different upstream IPs in X-Forwarded-For must share the same bucket and get throttled.
        """
        proxy_client_ip = "203.0.113.195"

        # Request 1 with spoofed header "1.1.1.1, 203.0.113.195"
        resp1 = self.client.get("/seats/", HTTP_X_FORWARDED_FOR=f"1.1.1.1, {proxy_client_ip}")
        self.assertEqual(resp1.status_code, 200)

        # Request 2 with spoofed header "2.2.2.2, 203.0.113.195"
        resp2 = self.client.get("/seats/", HTTP_X_FORWARDED_FOR=f"2.2.2.2, {proxy_client_ip}")
        self.assertEqual(resp2.status_code, 200)

        # Request 3 with spoofed header "3.3.3.3, 203.0.113.195"
        # Must be throttled because NUM_PROXIES=1 resolves both to proxy_client_ip
        resp3 = self.client.get("/seats/", HTTP_X_FORWARDED_FOR=f"3.3.3.3, {proxy_client_ip}")
        self.assertEqual(resp3.status_code, 429, "Spoofed upstream IP in X-Forwarded-For must not bypass rate limit")
        self.assertEqual(resp3.data.get("code"), "THROTTLED")

    @override_settings(
        REST_FRAMEWORK={
            "DEFAULT_THROTTLE_RATES": {
                "inventory_check": "2/min",
                "inventory_release": "2/min",
            },
            "EXCEPTION_HANDLER": "inventory.throttling.inventory_exception_handler",
        }
    )
    def test_fine_grained_scopes_check_and_release(self):
        """
        G4: check_reservation and release_seat obey their respective rate limits.
        """
        tid = uuid.uuid4()
        for _ in range(2):
            resp = self.client.get(f"/seats/check/?transaction_id={tid}")
            self.assertEqual(resp.status_code, 200)

        resp_check_3 = self.client.get(f"/seats/check/?transaction_id={tid}")
        self.assertEqual(resp_check_3.status_code, 429)

        # release_seat per user
        for _ in range(2):
            resp = self.client.post(
                "/seats/release/",
                {"transaction_id": str(tid), "user_id": "usr_rel_throttle"},
                format="json",
            )
            self.assertNotEqual(resp.status_code, 429)

        resp_rel_3 = self.client.post(
            "/seats/release/",
            {"transaction_id": str(tid), "user_id": "usr_rel_throttle"},
            format="json",
        )
        self.assertEqual(resp_rel_3.status_code, 429)
