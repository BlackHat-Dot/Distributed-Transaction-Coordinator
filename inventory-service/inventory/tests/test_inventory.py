import uuid
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch, MagicMock

from django.conf import settings
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from inventory.models import Event, Seat, Reservation


RESERVE_URL = "/seats/reserve/"
CONFIRM_URL = "/seats/confirm/"
RELEASE_URL = "/seats/release/"
CHECK_URL = "/reserve/check/"
EVENTS_URL = "/events/"
SEATS_URL = "/seats/"
SWEEP_URL = "/seats/sweep-expired/"
HEALTH_URL = "/health/"


def _create_event(name="Rock Concert", venue="Grand Arena"):
    return Event.objects.create(
        name=name,
        venue=venue,
        starts_at=timezone.now() + timedelta(days=7),
    )


def _create_seat(event, row="A", seat_number="1", price=500.00, status=Seat.Status.AVAILABLE, held_until=None, held_by=None):
    return Seat.objects.create(
        event=event,
        row=row,
        seat_number=seat_number,
        price=price,
        status=status,
        held_until=held_until,
        held_by_transaction_id=held_by,
    )


class HealthCheckTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_health_returns_200(self):
        resp = self.client.get(HEALTH_URL)
        self.assertEqual(resp.status_code, 200)


class EventAndSeatListTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = _create_event()
        self.seat1 = _create_seat(self.event, row="A", seat_number="1")
        self.seat2 = _create_seat(self.event, row="A", seat_number="2")

    def test_list_events_includes_nested_seats(self):
        resp = self.client.get(EVENTS_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["name"], "Rock Concert")
        self.assertEqual(len(resp.data[0]["seats"]), 2)

    def test_list_seats(self):
        resp = self.client.get(SEATS_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)

    def test_pricing_endpoint_returns_requested_seats(self):
        resp = self.client.post("/seats/pricing/", {"seat_ids": [self.seat1.id]}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["id"], self.seat1.id)

    def test_pricing_endpoint_returns_multiple_seats(self):
        resp = self.client.post("/seats/pricing/", {"seat_ids": [self.seat1.id, self.seat2.id]}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)

    def test_pricing_endpoint_missing_seat_ids_returns_400(self):
        resp = self.client.post("/seats/pricing/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.data)

    def test_pricing_endpoint_with_valid_internal_key_succeeds(self):
        from django.conf import settings
        resp = self.client.post(
            "/seats/pricing/",
            {"seat_ids": [self.seat1.id]},
            HTTP_X_INTERNAL_SERVICE_KEY=settings.INTERNAL_SERVICE_KEY,
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)

    def test_pricing_endpoint_with_invalid_internal_key_returns_403(self):
        resp = self.client.post(
            "/seats/pricing/",
            {"seat_ids": [self.seat1.id]},
            HTTP_X_INTERNAL_SERVICE_KEY="invalid-secret-key",
            format="json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Forbidden", resp.data.get("error", ""))


class ReserveValidationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = _create_event()
        self.seat = _create_seat(self.event)

    def test_missing_seat_id_returns_400(self):
        resp = self.client.post(
            RESERVE_URL,
            {"transaction_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("seat_ids", resp.data.get("error", ""))

    def test_invalid_seat_id_returns_400(self):
        resp = self.client.post(
            RESERVE_URL,
            {"seat_ids": ["invalid"], "transaction_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_missing_transaction_id_returns_400(self):
        resp = self.client.post(
            RESERVE_URL,
            {"seat_ids": [self.seat.id]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_invalid_uuid_returns_400(self):
        resp = self.client.post(
            RESERVE_URL,
            {"seat_ids": [self.seat.id], "transaction_id": "not-a-uuid"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_nonexistent_seat_returns_404(self):
        resp = self.client.post(
            RESERVE_URL,
            {"seat_ids": [99999], "transaction_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)


class ReserveSeatHoldTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = _create_event()
        self.seat = _create_seat(self.event, row="B", seat_number="5")

    def test_reserve_transitions_available_to_held_with_10m_expiry(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            RESERVE_URL,
            {"seat_ids": [self.seat.id], "transaction_id": tid},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["seat_ids"][0], self.seat.id)

        self.seat.refresh_from_db()
        self.assertEqual(self.seat.status, Seat.Status.HELD)
        self.assertEqual(str(self.seat.held_by_transaction_id), tid)
        self.assertIsNotNone(self.seat.held_until)
        remaining_seconds = (self.seat.held_until - timezone.now()).total_seconds()
        self.assertGreater(remaining_seconds, 580)
        self.assertLessEqual(remaining_seconds, 600)

        res = Reservation.objects.get(transaction_id=tid)
        self.assertEqual(res.status, Reservation.Status.RESERVED)
        self.assertEqual(res.seat_id, self.seat.id)

    def test_idempotent_reserve_same_transaction_returns_200(self):
        tid = str(uuid.uuid4())
        resp1 = self.client.post(
            RESERVE_URL,
            {"seat_ids": [self.seat.id], "transaction_id": tid},
            format="json",
        )
        self.assertEqual(resp1.status_code, 200)

        resp2 = self.client.post(
            RESERVE_URL,
            {"seat_ids": [self.seat.id], "transaction_id": tid},
            format="json",
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(Reservation.objects.filter(transaction_id=tid).count(), 1)

    def test_reserve_conflicts_if_already_held_by_different_transaction(self):
        tid1 = str(uuid.uuid4())
        tid2 = str(uuid.uuid4())

        resp1 = self.client.post(
            RESERVE_URL,
            {"seat_ids": [self.seat.id], "transaction_id": tid1},
            format="json",
        )
        self.assertEqual(resp1.status_code, 200)

        resp2 = self.client.post(
            RESERVE_URL,
            {"seat_ids": [self.seat.id], "transaction_id": tid2},
            format="json",
        )
        self.assertEqual(resp2.status_code, 409)
        self.assertIn("held", resp2.data["error"].lower())

    def test_reserve_conflicts_if_already_booked(self):
        self.seat.status = Seat.Status.BOOKED
        self.seat.save()

        tid = str(uuid.uuid4())
        resp = self.client.post(
            RESERVE_URL,
            {"seat_ids": [self.seat.id], "transaction_id": tid},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("booked", resp.data["error"].lower())


class ConfirmSeatTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = _create_event()
        self.seat = _create_seat(self.event, row="C", seat_number="10")

    def test_confirm_transitions_held_to_booked(self):
        tid = str(uuid.uuid4())
        self.client.post(
            RESERVE_URL,
            {"seat_ids": [self.seat.id], "transaction_id": tid},
            format="json",
        )

        resp = self.client.post(
            CONFIRM_URL,
            {"seat_ids": [self.seat.id], "transaction_id": tid},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

        self.seat.refresh_from_db()
        self.assertEqual(self.seat.status, Seat.Status.BOOKED)
        self.assertIsNone(self.seat.held_until)

        res = Reservation.objects.get(transaction_id=tid)
        self.assertEqual(res.status, Reservation.Status.BOOKED)

    def test_idempotent_confirm(self):
        tid = str(uuid.uuid4())
        self.client.post(RESERVE_URL, {"seat_ids": [self.seat.id], "transaction_id": tid}, format="json")
        resp1 = self.client.post(CONFIRM_URL, {"seat_ids": [self.seat.id], "transaction_id": tid}, format="json")
        self.assertEqual(resp1.status_code, 200)

        resp2 = self.client.post(CONFIRM_URL, {"seat_ids": [self.seat.id], "transaction_id": tid}, format="json")
        self.assertEqual(resp2.status_code, 200)

class ReleaseSeatTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = _create_event()
        self.seat = _create_seat(self.event, row="D", seat_number="1")

    def test_release_restores_held_seat_to_available(self):
        tid = str(uuid.uuid4())
        self.client.post(RESERVE_URL, {"seat_ids": [self.seat.id], "transaction_id": tid}, format="json")

        resp = self.client.post(RELEASE_URL, {"seat_ids": [self.seat.id], "transaction_id": tid}, format="json")
        self.assertEqual(resp.status_code, 200)

        self.seat.refresh_from_db()
        self.assertEqual(self.seat.status, Seat.Status.AVAILABLE)
        self.assertIsNone(self.seat.held_until)
        self.assertIsNone(self.seat.held_by_transaction_id)

    def test_idempotent_release(self):
        tid = str(uuid.uuid4())
        self.client.post(RESERVE_URL, {"seat_ids": [self.seat.id], "transaction_id": tid}, format="json")
        resp1 = self.client.post(RELEASE_URL, {"seat_ids": [self.seat.id], "transaction_id": tid}, format="json")
        self.assertEqual(resp1.status_code, 200)

        resp2 = self.client.post(RELEASE_URL, {"seat_ids": [self.seat.id], "transaction_id": tid}, format="json")
        self.assertEqual(resp2.status_code, 200)


class ReservationCheckTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = _create_event()
        self.seat = _create_seat(self.event)

    def test_check_nonexistent_returns_not_reserved(self):
        tid = str(uuid.uuid4())
        resp = self.client.get(CHECK_URL, {"transaction_id": tid})
        self.assertEqual(resp.status_code, 200)

    def test_check_reserved_returns_status(self):
        tid = str(uuid.uuid4())
        self.client.post(RESERVE_URL, {"seat_ids": [self.seat.id], "transaction_id": tid}, format="json")

        resp = self.client.get(CHECK_URL, {"transaction_id": tid})
        self.assertEqual(resp.status_code, 200)


class ConcurrencySeatStressTests(TransactionTestCase):
    def setUp(self):
        self.event = _create_event()
        self.seat = _create_seat(self.event, row="X", seat_number="99")

    def test_n_concurrent_reserves_at_same_seat_yield_single_winner(self):
        concurrency = 8
        results = []

        def do_reserve(worker_id):
            client = APIClient()
            tid = str(uuid.uuid4())
            try:
                resp = client.post(
                    RESERVE_URL,
                    {"seat_ids": [self.seat.id], "transaction_id": tid},
                    format="json",
                )
                return resp.status_code, resp.data
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(do_reserve, i) for i in range(concurrency)]
            for f in as_completed(futures):
                results.append(f.result())

        status_codes = [r[0] for r in results]
        self.assertEqual(
            status_codes.count(200),
            1,
            f"Expected exactly 1 winner with status 200, got: {status_codes}",
        )
        self.assertEqual(
            status_codes.count(409),
            concurrency - 1,
            f"Expected {concurrency - 1} conflict responses with status 409, got: {status_codes}",
        )

        self.seat.refresh_from_db()
        self.assertEqual(self.seat.status, Seat.Status.HELD)
        self.assertEqual(Reservation.objects.filter(seat=self.seat).count(), 1)


class RedisVsDbDivergenceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = _create_event()
        self.seat = _create_seat(self.event, row="F", seat_number="1")

    def test_stale_redis_key_does_not_block_available_seat_in_db(self):
        mock_redis = MagicMock()
        mock_redis.set.return_value = False

        with patch("inventory.views.get_redis_client", return_value=mock_redis):
            tid = str(uuid.uuid4())
            resp = self.client.post(
                RESERVE_URL,
                {"seat_ids": [self.seat.id], "transaction_id": tid},
                format="json",
            )
            self.assertEqual(resp.status_code, 200)
            self.seat.refresh_from_db()
            self.assertEqual(self.seat.status, Seat.Status.HELD)
            self.assertEqual(str(self.seat.held_by_transaction_id), tid)

    def test_evicted_redis_key_does_not_allow_double_booking_of_held_seat(self):
        active_tid = str(uuid.uuid4())
        self.seat.status = Seat.Status.HELD
        self.seat.held_until = timezone.now() + timedelta(minutes=5)
        self.seat.held_by_transaction_id = uuid.UUID(active_tid)
        self.seat.save()

        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with patch("inventory.views.get_redis_client", return_value=mock_redis):
            new_tid = str(uuid.uuid4())
            resp = self.client.post(
                RESERVE_URL,
                {"seat_ids": [self.seat.id], "transaction_id": new_tid},
                format="json",
            )
            self.assertEqual(resp.status_code, 409)
            self.assertIn("held", resp.data.get("error", "").lower())
            self.seat.refresh_from_db()
            self.assertEqual(str(self.seat.held_by_transaction_id), active_tid)

    def test_evicted_redis_key_does_not_allow_booking_of_already_booked_seat(self):
        self.seat.status = Seat.Status.BOOKED
        self.seat.save()

        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with patch("inventory.views.get_redis_client", return_value=mock_redis):
            tid = str(uuid.uuid4())
            resp = self.client.post(
                RESERVE_URL,
                {"seat_ids": [self.seat.id], "transaction_id": tid},
                format="json",
            )
            self.assertEqual(resp.status_code, 409)
            self.assertIn("booked", resp.data.get("error", "").lower())

    def test_redis_complete_outage_gracefully_falls_back_to_database(self):
        mock_redis = MagicMock()
        mock_redis.set.side_effect = Exception("Redis connection refused: 6379")

        with patch("inventory.views.get_redis_client", return_value=mock_redis):
            tid = str(uuid.uuid4())
            resp = self.client.post(
                RESERVE_URL,
                {"seat_ids": [self.seat.id], "transaction_id": tid},
                format="json",
            )
            self.assertEqual(resp.status_code, 200)
            self.seat.refresh_from_db()
            self.assertEqual(self.seat.status, Seat.Status.HELD)


class SeatModelAndEdgeCaseTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = _create_event(name="Jazz Night")
        self.seat1 = _create_seat(self.event, row="G", seat_number="1")
        self.seat2 = _create_seat(self.event, row="G", seat_number="2")

    def test_unique_seat_constraint_enforced_at_db_level(self):
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            _create_seat(self.event, row="G", seat_number="1")

    def test_confirm_seat_without_prior_reservation_returns_404(self):
        resp = self.client.post(
            CONFIRM_URL,
            {"seat_ids": [self.seat1.id], "transaction_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_confirm_seat_when_seat_already_booked_by_same_transaction_is_idempotent_200(self):
        tid = str(uuid.uuid4())
        self.client.post(RESERVE_URL, {"seat_ids": [self.seat1.id], "transaction_id": tid}, format="json")
        self.client.post(CONFIRM_URL, {"seat_ids": [self.seat1.id], "transaction_id": tid}, format="json")
        
        resp = self.client.post(CONFIRM_URL, {"seat_ids": [self.seat1.id], "transaction_id": tid}, format="json")
        self.assertEqual(resp.status_code, 200)

    def test_release_seat_for_nonexistent_reservation_returns_200_idempotent(self):
        resp = self.client.post(
            RELEASE_URL,
            {"seat_ids": [self.seat1.id], "transaction_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

class InventoryCorrelationIdMiddlewareTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_incoming_correlation_id_is_returned_in_response(self):
        cid = "inv-corr-id-" + str(uuid.uuid4())
        resp = self.client.get(HEALTH_URL, HTTP_X_CORRELATION_ID=cid)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get("X-Correlation-ID"), cid)

    def test_missing_correlation_id_generates_uuid(self):
        resp = self.client.get(HEALTH_URL)
        self.assertEqual(resp.status_code, 200)
        cid = resp.get("X-Correlation-ID")
        self.assertTrue(cid)
        uuid.UUID(cid)


class Batch2InventorySecurityAndRaceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = _create_event()
        self.seat = _create_seat(self.event, row="E", seat_number="1")
        self.valid_key = "dtc-internal-coordinator-secret-key-2026"

    def test_confirm_seat_fails_with_409_if_seat_held_by_another_transaction(self):
        tid1 = str(uuid.uuid4())
        tid2 = str(uuid.uuid4())
        # Reserve with tid1
        resp = self.client.post(RESERVE_URL, {"seat_ids": [self.seat.id], "transaction_id": tid1}, format="json")
        self.assertEqual(resp.status_code, 200)

        # Simulate another transaction re-holding or stealing the seat
        self.seat.refresh_from_db()
        self.seat.held_by_transaction_id = uuid.UUID(tid2)
        self.seat.save()

        # Confirm attempt by tid1 must be rejected with 409 HOLD_EXPIRED
        confirm_resp = self.client.post(CONFIRM_URL, {"seat_ids": [self.seat.id], "transaction_id": tid1}, format="json")
        self.assertEqual(confirm_resp.status_code, 409)
        self.assertEqual(confirm_resp.data.get("code"), "HOLD_EXPIRED")

    def test_confirm_seat_fails_with_409_if_hold_expired(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(RESERVE_URL, {"seat_ids": [self.seat.id], "transaction_id": tid}, format="json")
        self.assertEqual(resp.status_code, 200)

        # Expire the hold
        self.seat.refresh_from_db()
        self.seat.held_until = timezone.now() - timedelta(minutes=1)
        self.seat.save()

        # Confirm attempt must be rejected with 409 HOLD_EXPIRED
        confirm_resp = self.client.post(CONFIRM_URL, {"seat_ids": [self.seat.id], "transaction_id": tid}, format="json")
        self.assertEqual(confirm_resp.status_code, 409)
        self.assertEqual(confirm_resp.data.get("code"), "HOLD_EXPIRED")

    def test_internal_service_key_invalid_header_returns_403(self):
        tid = str(uuid.uuid4())
        for url in [RESERVE_URL, CONFIRM_URL, RELEASE_URL]:
            resp = self.client.post(
                url,
                {"seat_ids": [self.seat.id], "transaction_id": tid},
                format="json",
                HTTP_X_INTERNAL_SERVICE_KEY="invalid-attacker-key",
            )
            self.assertEqual(resp.status_code, 403, f"Expected 403 on {url} with invalid key")
            self.assertIn("Forbidden", resp.data.get("error", ""))

    def test_internal_service_key_valid_header_succeeds(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            RESERVE_URL,
            {"seat_ids": [self.seat.id], "transaction_id": tid},
            format="json",
            HTTP_X_INTERNAL_SERVICE_KEY=self.valid_key,
        )
        self.assertEqual(resp.status_code, 200)


class MaxActorHoldsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = _create_event()
        self.seats = [_create_seat(self.event, row="B", seat_number=str(i)) for i in range(1, 15)]

    def test_single_request_exceeding_max_holds_rejected_with_429(self):
        tid = str(uuid.uuid4())
        with self.settings(MAX_USER_HOLDS=5, MAX_ACTOR_HOLDS=5):
            seat_ids = [s.id for s in self.seats[:6]]
            resp = self.client.post(
                RESERVE_URL,
                {"seat_ids": seat_ids, "transaction_id": tid, "user_id": "usr_test_limit"},
                format="json",
            )
            self.assertEqual(resp.status_code, 429)
            self.assertEqual(resp.data.get("code"), "MAX_HOLDS_EXCEEDED")

    def test_accumulated_holds_exceeding_max_holds_rejected_with_429(self):
        with self.settings(MAX_USER_HOLDS=3, MAX_ACTOR_HOLDS=3):
            # First reservation of 2 seats succeeds
            tid1 = str(uuid.uuid4())
            resp1 = self.client.post(
                RESERVE_URL,
                {"seat_ids": [self.seats[0].id, self.seats[1].id], "transaction_id": tid1, "user_id": "usr_test_limit_2"},
                format="json",
            )
            self.assertEqual(resp1.status_code, 200)

            # Second reservation of 2 more seats exceeds limit (2 + 2 = 4 > 3)
            tid2 = str(uuid.uuid4())
            resp2 = self.client.post(
                RESERVE_URL,
                {"seat_ids": [self.seats[2].id, self.seats[3].id], "transaction_id": tid2, "user_id": "usr_test_limit_2"},
                format="json",
            )
            self.assertEqual(resp2.status_code, 429)
            self.assertEqual(resp2.data.get("code"), "MAX_HOLDS_EXCEEDED")


class SweepExpiredHoldsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = _create_event()
        self.seat1 = _create_seat(self.event, row="C", seat_number="1")
        self.seat2 = _create_seat(self.event, row="C", seat_number="2")

    def test_sweep_expired_holds_reclaims_seats(self):
        from django.core.management import call_command
        # Hold both seats
        tid = str(uuid.uuid4())
        resp = self.client.post(
            RESERVE_URL,
            {"seat_ids": [self.seat1.id, self.seat2.id], "transaction_id": tid},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

        # Manually expire seat1
        self.seat1.refresh_from_db()
        self.seat1.held_until = timezone.now() - timedelta(minutes=5)
        self.seat1.save()

        # Call management command
        call_command("sweep_expired_holds")

        self.seat1.refresh_from_db()
        self.seat2.refresh_from_db()
        self.assertEqual(self.seat1.status, Seat.Status.AVAILABLE)
        self.assertIsNone(self.seat1.held_until)
        self.assertIsNone(self.seat1.held_by_transaction_id)
        # Seat 2 should still be held
        self.assertEqual(self.seat2.status, Seat.Status.HELD)

    def test_sweep_expired_holds_endpoint(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(
            RESERVE_URL,
            {"seat_ids": [self.seat1.id], "transaction_id": tid},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

        self.seat1.refresh_from_db()
        self.seat1.held_until = timezone.now() - timedelta(minutes=1)
        self.seat1.save()

        resp = self.client.post(SWEEP_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("reclaimed_count"), 1)

        self.seat1.refresh_from_db()
        self.assertEqual(self.seat1.status, Seat.Status.AVAILABLE)


class BulkConfirmAndReleaseTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = _create_event()
        self.seats = [_create_seat(self.event, row="D", seat_number=str(i)) for i in range(1, 4)]

    def test_bulk_confirm_and_release(self):
        seat_ids = [s.id for s in self.seats]
        tid = str(uuid.uuid4())

        # Reserve all 3
        res = self.client.post(RESERVE_URL, {"seat_ids": seat_ids, "transaction_id": tid}, format="json")
        self.assertEqual(res.status_code, 200)

        # Confirm all 3 in bulk
        confirm_res = self.client.post(CONFIRM_URL, {"seat_ids": seat_ids, "transaction_id": tid}, format="json")
        self.assertEqual(confirm_res.status_code, 200)
        for s in Seat.objects.filter(id__in=seat_ids):
            self.assertEqual(s.status, Seat.Status.BOOKED)

        # Release all 3 in bulk
        release_res = self.client.post(RELEASE_URL, {"seat_ids": seat_ids, "transaction_id": tid}, format="json")
        self.assertEqual(release_res.status_code, 200)
        for s in Seat.objects.filter(id__in=seat_ids):
            self.assertEqual(s.status, Seat.Status.AVAILABLE)
            self.assertIsNone(s.held_by_transaction_id)


class ProductionReserveAuthEnforcementTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = _create_event()
        self.seat = _create_seat(self.event, row="E", seat_number="1")

    @patch("inventory.views.settings.DEBUG", False)
    @patch("inventory.views.settings.IS_TESTING", False)
    def test_production_rejects_unauthenticated_reserve(self):
        import sys
        with patch.object(sys, "argv", ["gunicorn"]):
            tid = str(uuid.uuid4())
            # Anonymous / missing user
            res = self.client.post(RESERVE_URL, {"seat_ids": [self.seat.id], "transaction_id": tid}, format="json")
            self.assertEqual(res.status_code, 401)
            self.assertEqual(res.data.get("code"), "AUTHENTICATION_REQUIRED")

    @patch("inventory.views.settings.DEBUG", False)
    @patch("inventory.views.settings.IS_TESTING", False)
    def test_production_accepts_authenticated_user_reserve(self):
        import sys
        with patch.object(sys, "argv", ["gunicorn"]):
            tid = str(uuid.uuid4())
            res = self.client.post(
                RESERVE_URL,
                {"seat_ids": [self.seat.id], "transaction_id": tid, "user_id": "usr_fan123", "user_email": "fan@test.io"},
                format="json"
            )
            self.assertEqual(res.status_code, 200)
            self.seat.refresh_from_db()
            self.assertEqual(self.seat.status, Seat.Status.HELD)
            self.assertEqual(self.seat.user_id, "usr_fan123")


class ConcurrentHoldQuotaTests(TransactionTestCase):
    def setUp(self):
        self.event = _create_event()
        self.seats_group1 = [
            _create_seat(self.event, row="X", seat_number=str(i)) for i in range(1, 4)
        ]
        self.seats_group2 = [
            _create_seat(self.event, row="Y", seat_number=str(i)) for i in range(1, 4)
        ]

    def test_conc01_concurrent_reservations_cannot_exceed_max_user_holds(self):
        """
        CONC-01: Proves that PostgreSQL transactional advisory locks prevent
        two concurrent requests from racing past MAX_USER_HOLDS.
        """
        user_id = "usr_concurrent_scalper"
        user_email = "scalper@test.io"

        def do_reserve(seat_group):
            client = APIClient()
            tid = str(uuid.uuid4())
            try:
                with patch.object(settings, "MAX_USER_HOLDS", 4):
                    resp = client.post(
                        RESERVE_URL,
                        {
                            "seat_ids": [s.id for s in seat_group],
                            "transaction_id": tid,
                            "user_id": user_id,
                            "user_email": user_email,
                        },
                        format="json",
                    )
                    return resp.status_code, resp.data
            finally:
                connection.close()

        with patch.object(settings, "MAX_USER_HOLDS", 4):
            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(do_reserve, self.seats_group1)
                f2 = executor.submit(do_reserve, self.seats_group2)
                results = [f1.result(), f2.result()]

        status_codes = [r[0] for r in results]
        self.assertIn(200, status_codes)
        self.assertIn(429, status_codes)

        # Verify held seat count in database is exactly 3 (within limit of 4), not 6
        held_count = Seat.objects.filter(user_id=user_id, status=Seat.Status.HELD).count()
        self.assertEqual(held_count, 3)




