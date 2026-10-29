from datetime import timedelta
from decimal import Decimal
import os
import uuid
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from coordinator.models import Transaction, SagaEvent


class CrashRecoverySweeperTests(TestCase):
    def setUp(self):
        self.tx_id = uuid.uuid4()
        self.past_time = timezone.now() - timedelta(minutes=10)

    def _create_stuck_txn(self, status, **kwargs):
        defaults = {
            "transaction_id": self.tx_id,
            "status": status,
            "seat_ids": [1],
            "quantity": 1,
            "amount": Decimal("100.00"),
            "user_id": "usr_test",
        }
        defaults.update(kwargs)
        txn = Transaction.objects.create(**defaults)
        # Update updated_at in past to make eligible for sweep
        Transaction.objects.filter(pk=txn.pk).update(updated_at=self.past_time)
        txn.refresh_from_db()
        return txn

    def test_fresh_transactions_ignored_by_recovery_sweep(self):
        """Transactions newer than recovery threshold must not be claimed or modified."""
        txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.PAYMENT_PROCESSING,
            seat_ids=[1],
            quantity=1,
            amount=Decimal("100.00"),
        )

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get") as mock_get:
            call_command("recovery_stuck_transactions", threshold_seconds=60)
            mock_get.assert_not_called()

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.PAYMENT_PROCESSING)

    def test_recover_stuck_payment_processing_forward_complete_if_charged(self):
        """Stuck in PAYMENT_PROCESSING: if payment charged & confirm succeeds, forward-complete to COMPLETED."""
        txn = self._create_stuck_txn(Transaction.Status.PAYMENT_PROCESSING)

        def fake_get(url, *args, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            return mock

        def fake_post(url, *args, **kwargs):
            mock = MagicMock()
            if "seats/confirm" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CONFIRMED"}'
                mock.json.return_value = {"status": "CONFIRMED"}
            return mock

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=fake_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=fake_post) as mock_post:
            call_command("recovery_stuck_transactions", threshold_seconds=60)

            # Assert refund was NOT called
            call_urls = [str(arg) for call in mock_post.call_args_list for arg in call.args if isinstance(arg, str)]
            self.assertFalse(any("payments/refund" in u for u in call_urls))

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPLETED)

        event = SagaEvent.objects.filter(
            transaction=txn,
            to_status=Transaction.Status.COMPLETED,
            actor=SagaEvent.ACTOR_RECOVERY,
        ).exists()
        self.assertTrue(event)

    def test_recover_stuck_payment_processing_compensates_when_hold_expired(self):
        """Stuck in PAYMENT_PROCESSING: if charged but confirm returns 409 (expired), refund & release."""
        txn = self._create_stuck_txn(Transaction.Status.PAYMENT_PROCESSING)
        calls = []

        def fake_get(url, *args, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            return mock

        def fake_post(url, *args, **kwargs):
            calls.append(url)
            mock = MagicMock()
            if "seats/confirm" in url:
                mock.status_code = 409
                mock.content = b'{"error": "Hold expired"}'
                mock.json.return_value = {"error": "Hold expired"}
            elif "payments/refund" in url:
                mock.status_code = 200
                mock.content = b'{"status": "REFUNDED"}'
                mock.json.return_value = {"status": "REFUNDED"}
            elif "seats/release" in url:
                mock.status_code = 200
                mock.content = b'{"message": "Released"}'
                mock.json.return_value = {"message": "Released"}
            return mock

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=fake_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=fake_post):
            call_command("recovery_stuck_transactions", threshold_seconds=60)

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED)
        self.assertTrue(any("payments/refund" in u for u in calls))
        self.assertTrue(any("seats/release" in u for u in calls))

    def test_recover_stuck_payment_processing_admin_reset_prohibits_auto_refund(self):
        """If confirm failed because of ADMIN_RESET, refund is prohibited; marked COMPENSATION_FAILED."""
        txn = self._create_stuck_txn(Transaction.Status.PAYMENT_PROCESSING)

        def fake_get(url, *args, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "CHARGED"}'
                mock.json.return_value = {"status": "CHARGED"}
            return mock

        def fake_post(url, *args, **kwargs):
            mock = MagicMock()
            if "seats/confirm" in url:
                mock.status_code = 409
                mock.content = b'{"error": "Seats reset by admin", "released_reason": "ADMIN_RESET"}'
                mock.json.return_value = {"error": "Seats reset by admin", "released_reason": "ADMIN_RESET"}
            return mock

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=fake_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=fake_post) as mock_post:
            call_command("recovery_stuck_transactions", threshold_seconds=60)

            # Auto-refund must NOT be issued
            call_urls = [str(arg) for call in mock_post.call_args_list for arg in call.args if isinstance(arg, str)]
            self.assertFalse(any("payments/refund" in u for u in call_urls))

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATION_FAILED)

        # Check manual_review_required flag in SagaEvent
        last_event = SagaEvent.objects.filter(transaction=txn).order_by("-seq").first()
        self.assertTrue(last_event.http_payload.get("manual_review_required"))

    def test_recover_stuck_payment_processing_not_charged_voids_and_releases(self):
        """Stuck in PAYMENT_PROCESSING: if NOT_CHARGED, void payment and release inventory -> COMPENSATED."""
        txn = self._create_stuck_txn(Transaction.Status.PAYMENT_PROCESSING)

        def fake_get(url, *args, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "NOT_CHARGED"}'
                mock.json.return_value = {"status": "NOT_CHARGED"}
            elif "reserve/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "INVENTORY_RESERVED"}'
                mock.json.return_value = {"status": "INVENTORY_RESERVED"}
            return mock

        def fake_post(url, *args, **kwargs):
            mock = MagicMock()
            if "payments/void" in url:
                mock.status_code = 200
                mock.content = b'{"status": "VOIDED"}'
                mock.json.return_value = {"status": "VOIDED"}
            elif "seats/release" in url:
                mock.status_code = 200
                mock.content = b'{"message": "Released"}'
                mock.json.return_value = {"message": "Released"}
            return mock

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=fake_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=fake_post) as mock_post:
            call_command("recovery_stuck_transactions", threshold_seconds=60)

            call_urls = [str(arg) for call in mock_post.call_args_list for arg in call.args if isinstance(arg, str)]
            self.assertFalse(any("payments/refund" in u for u in call_urls))

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED)

    def test_recover_stuck_payment_check_5xx_leaves_in_progress(self):
        """Payment check 5xx/transport error leaves transaction in RECOVERY_IN_PROGRESS for next sweep."""
        txn = self._create_stuck_txn(Transaction.Status.PAYMENT_PROCESSING)

        mock_get = MagicMock()
        mock_get.status_code = 503
        mock_get.content = b'{"error": "Downstream payment 503"}'
        mock_get.json.return_value = {"error": "Downstream payment 503"}

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", return_value=mock_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post") as mock_post:
            call_command("recovery_stuck_transactions", threshold_seconds=60)
            mock_post.assert_not_called()

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.RECOVERY_IN_PROGRESS)

    def test_recover_expired_holding_releases_and_marks_timeout(self):
        """Stuck in HOLDING past hold expiry checks payment/inventory, voids payment, and marks TIMEOUT."""
        txn = self._create_stuck_txn(Transaction.Status.HOLDING)
        past_hold = (timezone.now() - timedelta(minutes=5)).isoformat()
        SagaEvent.objects.create(
            transaction=txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.HOLDING,
            http_payload={"held_until": past_hold},
        )

        def fake_get(url, *args, **kwargs):
            mock = MagicMock()
            if "payments/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "NOT_CHARGED"}'
                mock.json.return_value = {"status": "NOT_CHARGED"}
            elif "reserve/check" in url:
                mock.status_code = 200
                mock.content = b'{"status": "RELEASED"}'
                mock.json.return_value = {"status": "RELEASED"}
            return mock

        def fake_post(url, *args, **kwargs):
            mock = MagicMock()
            mock.status_code = 200
            mock.content = b'{"status": "VOIDED"}'
            mock.json.return_value = {"status": "VOIDED"}
            return mock

        with patch("coordinator.management.commands.recovery_stuck_transactions.requests.get", side_effect=fake_get), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.post", side_effect=fake_post):
            call_command("recovery_stuck_transactions", threshold_seconds=60)

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.TIMEOUT)

    def test_recover_compensation_failed_sweeps_to_compensated(self):
        """Transactions in COMPENSATION_FAILED are swept via execute_compensation_sweep to COMPENSATED."""
        txn = self._create_stuck_txn(Transaction.Status.COMPENSATION_FAILED)

        def fake_sweep(transaction_id, **kwargs):
            Transaction.objects.filter(transaction_id=transaction_id).update(
                status=Transaction.Status.COMPENSATED,
                updated_at=timezone.now(),
            )
            return {"success": True, "status": Transaction.Status.COMPENSATED}

        with patch("coordinator.management.commands.recovery_stuck_transactions.execute_compensation_sweep", side_effect=fake_sweep):
            call_command("recovery_stuck_transactions", threshold_seconds=60)

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.COMPENSATED)

    def test_concurrent_sweep_skips_already_claimed_transaction(self):
        """If CAS claim fails (concurrent worker claimed first), sweeper skips without action."""
        txn = self._create_stuck_txn(Transaction.Status.PAYMENT_PROCESSING)

        with patch("coordinator.management.commands.recovery_stuck_transactions._set_status_with_event", return_value=False), \
             patch("coordinator.management.commands.recovery_stuck_transactions.requests.get") as mock_get:
            call_command("recovery_stuck_transactions", threshold_seconds=60)
            mock_get.assert_not_called()

        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.PAYMENT_PROCESSING)
