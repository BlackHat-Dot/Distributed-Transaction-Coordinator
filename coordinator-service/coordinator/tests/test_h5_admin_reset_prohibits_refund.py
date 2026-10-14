import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from coordinator.models import SagaEvent, Transaction
from coordinator.recovery import execute_compensation_sweep


class TestH5AdminResetProhibitsRefund(TestCase):
    def setUp(self):
        self.tx_id = uuid.uuid4()
        self.sse_patcher = patch("coordinator.saga_log._publish_transaction_update")
        self.sse_patcher.start()

    def tearDown(self):
        self.sse_patcher.stop()

    def test_execute_compensation_sweep_prohibits_refund_on_admin_reset_confirm_payload(self):
        txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.RECOVERY_IN_PROGRESS,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )

        refund_called = []

        def mock_get(url, *args, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            return mock

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "confirm" in url:
                mock.status_code = 409
                mock.content = b'{"error": "Hold expired", "released_reason": "ADMIN_RESET"}'
                mock.json.return_value = {
                    "error": "Hold expired",
                    "released_reason": "ADMIN_RESET",
                }
            elif "refund" in url:
                refund_called.append(url)
                mock.status_code = 200
                mock.content = b'{"status": "REFUNDED"}'
                mock.json.return_value = {"status": "REFUNDED"}
            else:
                mock.status_code = 200
                mock.content = b'{"status": "RELEASED"}'
                mock.json.return_value = {"status": "RELEASED"}
            return mock

        outcome = execute_compensation_sweep(
            transaction_id=self.tx_id,
            actor=SagaEvent.ACTOR_RETRY,
            original_status=Transaction.Status.COMPENSATION_FAILED,
            get_func=mock_get,
            post_func=mock_post,
        )

        self.assertFalse(outcome["success"])
        self.assertEqual(outcome["status_code"], 409)
        self.assertTrue(outcome["manual_review_required"])
        self.assertEqual(outcome["released_reason"], "ADMIN_RESET")
        self.assertEqual(len(refund_called), 0, "Refund must NOT be called for ADMIN_RESET")

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATION_FAILED)

        # Check SagaEvent log
        latest_event = SagaEvent.objects.filter(transaction=txn).order_by("-id").first()
        self.assertIsNotNone(latest_event)
        self.assertTrue(latest_event.http_payload.get("manual_review_required"))
        self.assertEqual(latest_event.http_payload.get("released_reason"), "ADMIN_RESET")

    def test_execute_compensation_sweep_prohibits_refund_on_admin_reset_check_payload(self):
        Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.RECOVERY_IN_PROGRESS,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )

        refund_called = []

        def mock_get(url, *args, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            elif "reserve/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "INVENTORY_RELEASED", "released_reason": "ADMIN_RESET"}'
                mock.json.return_value = {
                    "status": "INVENTORY_RELEASED",
                    "released_reason": "ADMIN_RESET",
                }
            return mock

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "confirm" in url:
                # 409 without released_reason, so coordinator falls back to reserve/check
                mock.status_code = 409
                mock.content = b'{"error": "Hold expired"}'
                mock.json.return_value = {"error": "Hold expired"}
            elif "refund" in url:
                refund_called.append(url)
                mock.status_code = 200
                mock.content = b'{"status": "REFUNDED"}'
                mock.json.return_value = {"status": "REFUNDED"}
            else:
                mock.status_code = 200
                mock.content = b'{"status": "RELEASED"}'
                mock.json.return_value = {"status": "RELEASED"}
            return mock

        outcome = execute_compensation_sweep(
            transaction_id=self.tx_id,
            actor=SagaEvent.ACTOR_RETRY,
            original_status=Transaction.Status.COMPENSATION_FAILED,
            get_func=mock_get,
            post_func=mock_post,
        )

        self.assertFalse(outcome["success"])
        self.assertEqual(outcome["status_code"], 409)
        self.assertTrue(outcome["manual_review_required"])
        self.assertEqual(len(refund_called), 0, "Refund must NOT be called for ADMIN_RESET")

    def test_recovery_command_prohibits_refund_when_confirm_fails_with_admin_reset(self):
        txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.PAYMENT_PROCESSING,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )
        past_time = timezone.now() - timedelta(minutes=10)
        Transaction.objects.filter(transaction_id=self.tx_id).update(updated_at=past_time)

        refund_called = []

        def mock_get(url, *args, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            return mock

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "confirm" in url:
                mock.status_code = 409
                mock.content = b'{"error": "Seat hold reset", "released_reason": "ADMIN_RESET"}'
                mock.json.return_value = {
                    "error": "Seat hold reset",
                    "released_reason": "ADMIN_RESET",
                }
            elif "refund" in url:
                refund_called.append(url)
                mock.status_code = 200
                mock.content = b'{"status": "REFUNDED"}'
                mock.json.return_value = {"status": "REFUNDED"}
            return mock

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=mock_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=mock_post):
            call_command("recovery_stuck_transactions", threshold_seconds=30)

        self.assertEqual(len(refund_called), 0, "Refund must NOT be called")
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATION_FAILED)

        latest_event = SagaEvent.objects.filter(transaction=txn).order_by("-id").first()
        self.assertIsNotNone(latest_event)
        self.assertTrue(latest_event.http_payload.get("manual_review_required"))

    def test_recovery_command_prohibits_refund_when_void_reveals_charge_and_inventory_admin_reset(self):
        txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.STARTED,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )
        past_time = timezone.now() - timedelta(minutes=10)
        Transaction.objects.filter(transaction_id=self.tx_id).update(updated_at=past_time)

        refund_called = []

        def mock_get(url, *args, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "NOT_CHARGED"}'
                mock.json.return_value = {"status": "NOT_CHARGED"}
            elif "reserve/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "INVENTORY_RELEASED", "released_reason": "ADMIN_RESET"}'
                mock.json.return_value = {
                    "status": "INVENTORY_RELEASED",
                    "released_reason": "ADMIN_RESET",
                }
            return mock

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "void" in url:
                # Late charge landed before void
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            elif "refund" in url:
                refund_called.append(url)
                mock.status_code = 200
                mock.content = b'{"status": "REFUNDED"}'
                mock.json.return_value = {"status": "REFUNDED"}
            return mock

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=mock_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=mock_post):
            call_command("recovery_stuck_transactions", threshold_seconds=30)

        self.assertEqual(len(refund_called), 0, "Refund must NOT be called")
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATION_FAILED)

    def test_execute_compensation_sweep_allows_refund_on_expired_hold(self):
        txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.RECOVERY_IN_PROGRESS,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )

        refund_called = []

        def mock_get(url, *args, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            return mock

        def mock_post(url, *args, **kwargs):
            mock = MagicMock()
            if "confirm" in url:
                mock.status_code = 409
                mock.content = b'{"error": "Hold expired", "released_reason": "EXPIRED_HOLD"}'
                mock.json.return_value = {
                    "error": "Hold expired",
                    "released_reason": "EXPIRED_HOLD",
                }
            elif "refund" in url:
                refund_called.append(url)
                mock.status_code = 200
                mock.content = b'{"status": "REFUNDED"}'
                mock.json.return_value = {"status": "REFUNDED"}
            else:
                mock.status_code = 200
                mock.content = b'{"status": "RELEASED"}'
                mock.json.return_value = {"status": "RELEASED"}
            return mock

        outcome = execute_compensation_sweep(
            transaction_id=self.tx_id,
            actor=SagaEvent.ACTOR_RETRY,
            original_status=Transaction.Status.COMPENSATION_FAILED,
            get_func=mock_get,
            post_func=mock_post,
        )

        self.assertTrue(outcome["success"])
        self.assertEqual(len(refund_called), 1, "Refund MUST be called for EXPIRED_HOLD")
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED_ON_RETRY)
