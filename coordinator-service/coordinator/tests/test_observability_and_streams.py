from decimal import Decimal
import json
from unittest.mock import MagicMock, patch
import uuid

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from coordinator.auth_views import _create_jwt
from coordinator.models import Transaction, SagaEvent


class ObservabilityAndStreamsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        cache.clear()
        self.user_id = "usr_stream_tester"
        self.user_email = "streamer@gmail.com"
        self.jwt_token = _create_jwt(self.user_id, self.user_email, session_id="sid_obs")
        self.auth_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.jwt_token}"}

        self.staff_key = getattr(settings, "STAFF_API_KEY", "test-staff-key")
        self.staff_headers = {"HTTP_X_STAFF_KEY": self.staff_key}

        self.tx_id = uuid.uuid4()
        self.txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            user_id=self.user_id,
            user_email=self.user_email,
            status=Transaction.Status.COMPLETED,
            seat_ids=[10, 11],
            quantity=2,
            amount=Decimal("200.00"),
        )

        # Create saga audit events
        SagaEvent.objects.create(
            transaction=self.txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.HOLDING,
            actor=SagaEvent.ACTOR_REQUEST,
        )
        SagaEvent.objects.create(
            transaction=self.txn,
            seq=2,
            from_status=Transaction.Status.HOLDING,
            to_status=Transaction.Status.PAYMENT_PROCESSING,
            actor=SagaEvent.ACTOR_REQUEST,
        )
        SagaEvent.objects.create(
            transaction=self.txn,
            seq=3,
            from_status=Transaction.Status.PAYMENT_PROCESSING,
            to_status=Transaction.Status.COMPLETED,
            actor=SagaEvent.ACTOR_REQUEST,
        )

    def tearDown(self):
        cache.clear()

    def test_get_transaction_ownership_and_staff_access(self):
        """get_transaction enforces strict ownership: 401 unauth, 403 non-owner, 200 owner/staff."""
        url = f"/transactions/{self.tx_id}/"

        # 1. Unauthenticated -> 401
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 401)

        # 2. Non-owner -> 403
        other_jwt = _create_jwt("usr_other", "other@gmail.com")
        resp = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {other_jwt}")
        self.assertEqual(resp.status_code, 403)

        # 3. Owner -> 200
        resp = self.client.get(url, **self.auth_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["transaction_id"], str(self.tx_id))
        self.assertEqual(resp.data["status"], Transaction.Status.COMPLETED)

        # 4. Staff key -> 200 regardless of owner
        with patch.dict("os.environ", {"STAFF_API_KEY": self.staff_key}):
            resp = self.client.get(url, **self.staff_headers)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data["transaction_id"], str(self.tx_id))

    def test_list_transaction_events_audit_log(self):
        """list_transaction_events returns ordered chronological saga audit events."""
        url = f"/transactions/{self.tx_id}/events/"

        # Non-owner forbidden
        other_jwt = _create_jwt("usr_other", "other@gmail.com")
        resp = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {other_jwt}")
        self.assertEqual(resp.status_code, 403)

        # Owner gets full audit trail
        resp = self.client.get(url, **self.auth_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["transaction_id"], str(self.tx_id))
        self.assertEqual(len(resp.data["events"]), 3)

        # Verify ordering and seq numbers
        events = resp.data["events"]
        self.assertEqual(events[0]["seq"], 1)
        self.assertEqual(events[0]["to_status"], Transaction.Status.HOLDING)
        self.assertEqual(events[2]["seq"], 3)
        self.assertEqual(events[2]["to_status"], Transaction.Status.COMPLETED)

    def test_list_transactions_feed_user_and_staff_isolation(self):
        """User only sees own transactions in feed; staff sees all transactions."""
        other_tx = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            user_id="usr_other",
            user_email="other@gmail.com",
            status=Transaction.Status.HOLDING,
            seat_ids=[20],
            amount=Decimal("50.00"),
        )

        # Regular user query
        resp = self.client.get("/transactions/all/", **self.auth_headers)
        self.assertEqual(resp.status_code, 200)
        tx_ids = [t["transaction_id"] for t in resp.data["results"]]
        self.assertIn(str(self.tx_id), tx_ids)
        self.assertNotIn(str(other_tx.transaction_id), tx_ids)

        # Staff query
        with patch.dict("os.environ", {"STAFF_API_KEY": self.staff_key}):
            resp = self.client.get("/transactions/all/", **self.staff_headers)
            self.assertEqual(resp.status_code, 200)
            tx_ids = [t["transaction_id"] for t in resp.data["results"]]
            self.assertIn(str(self.tx_id), tx_ids)
            self.assertIn(str(other_tx.transaction_id), tx_ids)

    def test_transaction_stream_sse_terminal_state(self):
        """SSE stream for terminal transaction emits immediate terminal event payload and closes."""
        url = f"/transactions/{self.tx_id}/stream/"

        # Unauthenticated -> 401
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 401)

        # Non-owner -> 403
        other_jwt = _create_jwt("usr_other", "other@gmail.com")
        resp = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {other_jwt}")
        self.assertEqual(resp.status_code, 403)

        # Owner -> StreamingHttpResponse with text/event-stream
        with patch("django.db.close_old_connections"):
            resp = self.client.get(url, **self.auth_headers)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp["Content-Type"], "text/event-stream")

            # Read streaming generator output
            content = b"".join(resp.streaming_content).decode("utf-8")
            self.assertTrue(content.startswith("data: "))
            payload = json.loads(content.replace("data: ", "").strip())
            self.assertEqual(payload["transaction_id"], str(self.tx_id))
            self.assertEqual(payload["status"], Transaction.Status.COMPLETED)
            self.assertTrue(payload["is_terminal"])

    def test_trigger_recovery_sweep_requires_staff_key(self):
        """trigger_recovery_sweep requires staff key and runs stuck transaction sweep."""
        url = "/recovery/sweep/"

        # Missing staff key -> 401
        resp = self.client.post(url, format="json")
        self.assertEqual(resp.status_code, 401)

        # Invalid staff key -> 403
        with patch.dict("os.environ", {"STAFF_API_KEY": self.staff_key}):
            resp = self.client.post(url, HTTP_X_STAFF_KEY="wrong-key", format="json")
            self.assertEqual(resp.status_code, 403)

            # Valid staff key succeeds -> 200
            with patch("django.core.management.call_command") as mock_cmd:
                resp = self.client.post(url, **self.staff_headers, format="json")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.data.get("message"), "Recovery sweep complete")
                mock_cmd.assert_called_once()

    def test_trigger_recovery_sweep_concurrent_lock_conflict(self):
        """When distributed lock is held by another worker, sweep returns 409 RECOVERY_ALREADY_IN_PROGRESS."""
        mock_redis = MagicMock()
        mock_redis.set.return_value = False  # Lock acquisition failed

        with patch.dict("os.environ", {"STAFF_API_KEY": self.staff_key}), \
             patch("coordinator.tasks.get_redis_client", return_value=mock_redis):
            resp = self.client.post("/recovery/sweep/", **self.staff_headers, format="json")
            self.assertEqual(resp.status_code, 409)
            self.assertEqual(resp.data.get("code"), "RECOVERY_ALREADY_IN_PROGRESS")

    def test_recovery_status_metrics(self):
        """recovery_status returns Celery Beat schedule timing and Redis connection status."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = b"1700000000.0"

        with patch("coordinator.tasks.get_redis_client", return_value=mock_redis):
            resp = self.client.get("/recovery/status/")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data.get("service"), "celery_beat")
            self.assertTrue(resp.data.get("redis_connected"))
            self.assertIn("status", resp.data)
