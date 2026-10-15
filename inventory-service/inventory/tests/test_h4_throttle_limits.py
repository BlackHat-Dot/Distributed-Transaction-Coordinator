import time
import uuid
from decimal import Decimal
import jwt
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from inventory.models import Event, Seat


class TestH4ThrottleLimits(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.event = Event.objects.create(name="H4 On-Sale Tour")
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
                "inventory_reserve_anon": "5/min",
                "inventory_anon": "5/min",
                "inventory_reserve": "30/min",
            },
            "EXCEPTION_HANDLER": "inventory.throttling.inventory_exception_handler",
        }
    )
    def test_20_distinct_users_sharing_single_ip_not_blocked_by_per_ip_anon_limit(self):
        """
        H4: 20 distinct users sharing the same NAT IP address on /seats/reserve/
        must NOT be blocked by the per-IP anon limit (5/min).
        Authenticated requests are throttled per user, not per IP.
        """
        shared_ip = "198.51.100.55"

        for i in range(20):
            user_id = f"fan_user_{i}"
            tid = str(uuid.uuid4())
            resp = self.client.post(
                "/seats/reserve/",
                {
                    "seat_ids": [self.seat.id],
                    "transaction_id": tid,
                    "user_id": user_id,
                },
                format="json",
                HTTP_X_FORWARDED_FOR=shared_ip,
                REMOTE_ADDR=shared_ip,
            )
            # Must NOT be 429 (first is 200, subsequent are 409 conflict because seat is held)
            self.assertNotEqual(
                resp.status_code,
                429,
                f"User {i} ({user_id}) from shared IP {shared_ip} was improperly throttled with 429 by anon IP limit!",
            )

    @override_settings(
        REST_FRAMEWORK={
            "NUM_PROXIES": 1,
            "DEFAULT_THROTTLE_RATES": {
                "inventory_reserve_anon": "5/min",
                "inventory_anon": "5/min",
                "inventory_reserve": "30/min",
            },
            "EXCEPTION_HANDLER": "inventory.throttling.inventory_exception_handler",
        },
        JWT_SIGNING_KEY="h4-test-jwt-key-minimum-32-bytes-secure!",
    )
    def test_20_jwt_authenticated_users_sharing_single_ip_not_blocked_by_per_ip_anon_limit(self):
        """
        H4: 20 distinct JWT-authenticated users sharing the same NAT IP address
        must NOT be blocked by the 5/min per-IP anon limit.
        """
        shared_ip = "198.51.100.77"
        now = int(time.time())

        for i in range(20):
            token = jwt.encode(
                {
                    "user_id": f"jwt_user_{i}",
                    "user_email": f"user_{i}@example.com",
                    "exp": now + 3600,
                },
                "h4-test-jwt-key-minimum-32-bytes-secure!",
                algorithm="HS256",
            )
            tid = str(uuid.uuid4())
            resp = self.client.post(
                "/seats/reserve/",
                {
                    "seat_ids": [self.seat.id],
                    "transaction_id": tid,
                },
                format="json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                HTTP_X_FORWARDED_FOR=shared_ip,
                REMOTE_ADDR=shared_ip,
            )
            self.assertNotEqual(
                resp.status_code,
                429,
                f"JWT User {i} from shared IP {shared_ip} was improperly throttled with 429 by anon IP limit!",
            )

    @override_settings(
        REST_FRAMEWORK={
            "NUM_PROXIES": 1,
            "DEFAULT_THROTTLE_RATES": {
                "inventory_reserve_anon": "5/min",
                "inventory_anon": "5/min",
                "inventory_reserve": "30/min",
            },
            "EXCEPTION_HANDLER": "inventory.throttling.inventory_exception_handler",
        }
    )
    def test_unauthenticated_requests_hit_per_ip_anon_limit(self):
        """
        H4: Unauthenticated requests from the same IP address DO hit the per-IP anon limit.
        """
        anon_ip = "198.51.100.99"

        for i in range(5):
            resp = self.client.post(
                "/seats/reserve/",
                {
                    "seat_ids": [self.seat.id],
                    "transaction_id": str(uuid.uuid4()),
                },
                format="json",
                HTTP_X_FORWARDED_FOR=anon_ip,
                REMOTE_ADDR=anon_ip,
            )
            self.assertNotEqual(resp.status_code, 429, f"Anon request {i+1} must be permitted under the 5/min limit")

        # 6th request from same IP exceeds 5/min limit
        resp6 = self.client.post(
            "/seats/reserve/",
            {
                "seat_ids": [self.seat.id],
                "transaction_id": str(uuid.uuid4()),
            },
            format="json",
            HTTP_X_FORWARDED_FOR=anon_ip,
            REMOTE_ADDR=anon_ip,
        )
        self.assertEqual(resp6.status_code, 429, "6th unauthenticated request must be throttled with 429")
        self.assertEqual(resp6.data.get("code"), "THROTTLED")

    @override_settings(
        REST_FRAMEWORK={
            "NUM_PROXIES": 1,
            "DEFAULT_THROTTLE_RATES": {
                "inventory_release_anon": "5/min",
                "inventory_anon": "5/min",
                "inventory_release": "30/min",
            },
            "EXCEPTION_HANDLER": "inventory.throttling.inventory_exception_handler",
        }
    )
    def test_release_route_20_distinct_users_sharing_single_ip_not_blocked(self):
        """
        H4: 20 distinct users sharing the same NAT IP address on /seats/release/
        must NOT be blocked by the 5/min per-IP anon limit.
        """
        shared_ip = "198.51.100.88"

        for i in range(20):
            user_id = f"rel_user_{i}"
            tid = str(uuid.uuid4())
            resp = self.client.post(
                "/seats/release/",
                {
                    "transaction_id": tid,
                    "user_id": user_id,
                },
                format="json",
                HTTP_X_FORWARDED_FOR=shared_ip,
                REMOTE_ADDR=shared_ip,
            )
            self.assertNotEqual(
                resp.status_code,
                429,
                f"Release User {i} from shared IP {shared_ip} was improperly throttled with 429 by anon IP limit!",
            )
