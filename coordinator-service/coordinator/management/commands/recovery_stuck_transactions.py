import logging
import os
import requests
from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from django.core.management.base import BaseCommand
from coordinator.models import Transaction, SagaEvent
from coordinator.saga_log import _set_status_with_event
from coordinator.recovery import execute_compensation_sweep

logger = logging.getLogger(__name__)

INVENTORY_BASE_URL = os.getenv("INVENTORY_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
PAYMENT_BASE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")

PAYMENT_CHECK_URL = f"{PAYMENT_BASE_URL}/payments/check/"
PAYMENT_REFUND_URL = f"{PAYMENT_BASE_URL}/payments/refund/"
PAYMENT_VOID_URL = f"{PAYMENT_BASE_URL}/payments/void/"
INVENTORY_CHECK_URL = f"{INVENTORY_BASE_URL}/reserve/check/"
INVENTORY_CONFIRM_URL = f"{INVENTORY_BASE_URL}/seats/confirm/"
REQUEST_CONNECT_TIMEOUT = float(os.getenv("REQUEST_CONNECT_TIMEOUT", "2.0"))
REQUEST_READ_TIMEOUT = float(os.getenv("REQUEST_READ_TIMEOUT", os.getenv("REQUEST_TIMEOUT", "5.0")))
REQUEST_TIMEOUT = (REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT)
INTERNAL_SERVICE_KEY = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY")


PAYMENT_CHARGED = "CHARGED"
PAYMENT_NOT_CHARGED = "NOT_CHARGED"

INVENTORY_RESERVED = Transaction.Status.INVENTORY_RESERVED
INVENTORY_RELEASED = Transaction.Status.INVENTORY_RELEASED
INVENTORY_NOT_RESERVED = Transaction.Status.NOT_RESERVED

# Single source of truth from Transaction model
TERMINAL_STATUSES = Transaction.TERMINAL_STATUSES
RECOVERABLE_STATUSES = Transaction.RECOVERABLE_STATUSES



class Command(BaseCommand):
    help = "Recovers transactions stuck in non-terminal states due to coordinator crashes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--threshold-seconds",
            type=int,
            default=int(os.getenv("RECOVERY_THRESHOLD_SECONDS", "60")),
            help="Age in seconds before a stuck transaction is eligible for recovery sweep (default: 60s).",
        )

    def handle(self, *args, **options):
        threshold_seconds = options.get("threshold_seconds")
        if threshold_seconds is None:
            threshold_seconds = int(os.getenv("RECOVERY_THRESHOLD_SECONDS", "60"))

        threshold = timezone.now() - timedelta(seconds=threshold_seconds)

        self.recover_payment_processing(threshold)
        self.recover_reserved(threshold)
        self.recover_holding(threshold)
        self.recover_started(threshold)
        self.recover_timeouts(threshold)
        self.recover_compensating(threshold)
        self.recover_compensation_failed(threshold)
        self.recover_retry_compensation_failed(threshold)

        self.recover_in_progress(threshold)

        self.stdout.write(self.style.SUCCESS("Recovery sweep complete."))

    def _claim(self, from_status, threshold):
        candidates = list(
            Transaction.objects.filter(
                status=from_status,
                updated_at__lt=threshold,
            ).values_list('transaction_id', flat=True)
        )

        claimed_ids = []
        for transaction_id in candidates:
            claimed = _set_status_with_event(
                transaction_id=transaction_id,
                to_status=Transaction.Status.RECOVERY_IN_PROGRESS,
                expected_status=from_status,
                actor=SagaEvent.ACTOR_RECOVERY,
                http_status_code=None,
                http_payload=None,
            )

            if not claimed:
                self.stdout.write(
                    f"  {from_status} {transaction_id}: already claimed, skipping."
                )
                continue

            claimed_ids.append(transaction_id)

        return claimed_ids

    def recover_payment_processing(self, threshold):

        for transaction_id in self._claim(Transaction.Status.PAYMENT_PROCESSING, threshold):
            self.stdout.write(
                f"Recovering {transaction_id} claimed from PAYMENT_PROCESSING..."
            )
            self._resolve_transaction(transaction_id)

    def recover_reserved(self, threshold):

        for transaction_id in self._claim(Transaction.Status.INVENTORY_RESERVED, threshold):
            self.stdout.write(
                f"Recovering {transaction_id} claimed from INVENTORY_RESERVED..."
            )
            self._resolve_transaction(transaction_id)

    def recover_holding(self, threshold=None):
        from django.utils.dateparse import parse_datetime
        from django.db.models import Prefetch
        now = timezone.now()
        HOLD_GRACE = timedelta(seconds=30)
        # Default hold TTL matches inventory service real hold TTL: 10 minutes
        hold_ttl = getattr(settings, "SEAT_HOLD_DURATION", timedelta(minutes=10))
        if isinstance(hold_ttl, (int, float)):
            hold_ttl = timedelta(seconds=hold_ttl)

        # (c) Fetch held_until with one single prefetch query across all HOLDING rows
        holding_txns = Transaction.objects.filter(
            status=Transaction.Status.HOLDING
        ).prefetch_related(
            Prefetch(
                "events",
                queryset=SagaEvent.objects.filter(to_status=Transaction.Status.HOLDING).order_by("-seq"),
                to_attr="holding_events",
            )
        )

        candidates = []
        for txn in holding_txns:
            holding_event = txn.holding_events[0] if getattr(txn, "holding_events", None) else None
            held_until_dt = None
            if holding_event and holding_event.http_payload:
                raw_held_until = holding_event.http_payload.get("held_until")
                if raw_held_until:
                    try:
                        dt = (
                            parse_datetime(raw_held_until)
                            if isinstance(raw_held_until, str)
                            else raw_held_until
                        )
                        if dt and timezone.is_naive(dt):
                            dt = timezone.make_aware(dt)
                        held_until_dt = dt
                    except Exception:
                        pass

            if held_until_dt is not None:
                # (b) Add 30s grace: treated as expired only at held_until + 30s
                if now >= held_until_dt + HOLD_GRACE:
                    candidates.append(txn.transaction_id)
            else:
                # Fallback hold TTL (10m) from created_at + 30s grace
                if now >= txn.created_at + hold_ttl + HOLD_GRACE:
                    candidates.append(txn.transaction_id)

        claimed_ids = []
        for transaction_id in candidates:
            claimed = _set_status_with_event(
                transaction_id=transaction_id,
                to_status=Transaction.Status.RECOVERY_IN_PROGRESS,
                expected_status=Transaction.Status.HOLDING,
                actor=SagaEvent.ACTOR_RECOVERY,
                http_status_code=None,
                http_payload=None,
            )
            if not claimed:
                continue
            claimed_ids.append(transaction_id)

        for transaction_id in claimed_ids:
            self.stdout.write(
                f"Recovering {transaction_id} claimed from HOLDING (expired hold)..."
            )
            self._resolve_transaction(transaction_id)


    def recover_started(self, threshold):

        for transaction_id in self._claim(Transaction.Status.STARTED, threshold):
            self.stdout.write(
                f"Recovering {transaction_id} claimed from STARTED..."
            )
            self._resolve_transaction(transaction_id)

    def recover_timeouts(self, threshold):
        timeout_statuses = [
            Transaction.Status.INVENTORY_TIMEOUT,
            Transaction.Status.PAYMENT_TIMEOUT,
            Transaction.Status.INVENTORY_CONNECTION_ERROR,
            Transaction.Status.PAYMENT_CONNECTION_ERROR,
        ]
        for st in timeout_statuses:
            for transaction_id in self._claim(st, threshold):
                self.stdout.write(
                    f"Recovering {transaction_id} claimed from {st}..."
                )
                self._resolve_transaction(transaction_id)

    def recover_compensating(self, threshold):

        for transaction_id in self._claim(Transaction.Status.COMPENSATING, threshold):
            self.stdout.write(
                f"Recovering {transaction_id} claimed from COMPENSATING..."
            )
            self._compensate(transaction_id)

    def recover_compensation_failed(self, threshold):

        for transaction_id in self._claim(Transaction.Status.COMPENSATION_FAILED, threshold):
            self.stdout.write(
                f"Recovering {transaction_id} claimed from COMPENSATION_FAILED..."
            )
            self._compensate(transaction_id)

    def recover_retry_compensation_failed(self, threshold):

        for transaction_id in self._claim(Transaction.Status.RETRY_COMPENSATION_FAILED, threshold):
            self.stdout.write(
                f"Recovering {transaction_id} claimed from RETRY_COMPENSATION_FAILED..."
            )
            self._compensate(transaction_id)


    def recover_in_progress(self, threshold):

        candidates = list(
            Transaction.objects.filter(
                status=Transaction.Status.RECOVERY_IN_PROGRESS,
                updated_at__lt=threshold,
            ).values_list('transaction_id', flat=True)
        )

        for transaction_id in candidates:
            claimed = Transaction.objects.filter(
                transaction_id=transaction_id,
                status=Transaction.Status.RECOVERY_IN_PROGRESS,
                updated_at__lt=threshold,
            ).update(
                status=Transaction.Status.RECOVERY_IN_PROGRESS,
                updated_at=timezone.now(),
            )

            if claimed != 1:
                self.stdout.write(
                    f"  RECOVERY_IN_PROGRESS {transaction_id}: already reclaimed, skipping."
                )
                continue

            self.stdout.write(
                f"Reclaiming {transaction_id} stuck in RECOVERY_IN_PROGRESS "
                f"(previous recovery worker likely crashed, or a prior sweep "
                f"couldn't resolve it)..."
            )
            self._resolve_transaction(transaction_id)


    def _resolve_transaction(self, transaction_id):
        headers = {
            "X-Correlation-ID": str(transaction_id),
            "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
            "X-Idempotency-Key": str(transaction_id),
        }
        try:
            payment_resp = requests.get(
                PAYMENT_CHECK_URL,
                params={"transaction_id": str(transaction_id)},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException:
            self._leave_in_progress(transaction_id, "payment check failed")
            return

        payment_status = payment_resp.json().get("status") if payment_resp.status_code == 200 else None

        if payment_status == PAYMENT_CHARGED:
            txn = Transaction.objects.filter(transaction_id=transaction_id).first()
            seat_ids = txn.seat_ids if txn else []

            # Check attempt counter or age cap for confirm retries
            retry_count = SagaEvent.objects.filter(
                transaction=txn,
                actor=SagaEvent.ACTOR_RECOVERY,
            ).count() if txn else 0
            age_minutes = ((timezone.now() - txn.created_at).total_seconds() / 60.0) if txn and txn.created_at else 0.0
            max_attempts = getattr(settings, "MAX_CONFIRM_RETRY_ATTEMPTS", 5)
            max_age_minutes = getattr(settings, "MAX_CONFIRM_RETRY_MINUTES", 60)

            if retry_count >= max_attempts or age_minutes >= max_age_minutes:
                logger.critical(
                    "CRITICAL ALERT: Transaction %s exceeded confirm retry limits (attempts=%d/%d, age=%.1fm/%dm). "
                    "Marked for MANUAL_REVIEW. Payment is CHARGED; NO auto-refund issued.",
                    transaction_id,
                    retry_count,
                    max_attempts,
                    age_minutes,
                    max_age_minutes,
                )
                self.stdout.write(
                    self.style.ERROR(
                        f"CRITICAL: {transaction_id} exceeded confirm retry limit (attempts={retry_count}/{max_attempts}, age={age_minutes:.1f}m/{max_age_minutes}m). "
                        "Marked for MANUAL REVIEW; NO auto-refund issued."
                    )
                )
                _set_status_with_event(
                    transaction_id=transaction_id,
                    to_status=Transaction.Status.PAYMENT_PROCESSING,
                    expected_status=Transaction.Status.RECOVERY_IN_PROGRESS,
                    actor=SagaEvent.ACTOR_RECOVERY,
                    http_status_code=None,
                    http_payload={
                        "error": "Exceeded confirm retry limit; marked for manual review.",
                        "manual_review_required": True,
                        "retry_count": retry_count,
                        "age_minutes": age_minutes,
                        "payment_status": "CHARGED",
                    },
                )
                return

            # Before completing, confirm inventory
            try:
                confirm_resp = requests.post(
                    INVENTORY_CONFIRM_URL,
                    json={"seat_ids": seat_ids, "transaction_id": str(transaction_id)},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.exceptions.RequestException:
                # Confirm transport error (Timeout/ConnectionError): leave recoverable, revert claim
                _set_status_with_event(
                    transaction_id=transaction_id,
                    to_status=Transaction.Status.PAYMENT_PROCESSING,
                    expected_status=Transaction.Status.RECOVERY_IN_PROGRESS,
                    actor=SagaEvent.ACTOR_RECOVERY,
                    http_status_code=None,
                    http_payload={"error": "Inventory confirm transport error during recovery; claim reverted."},
                )
                self.stdout.write(
                    self.style.WARNING(
                        f"  -> {transaction_id}: inventory confirm network/timeout error; claim reverted to PAYMENT_PROCESSING for retry."
                    )
                )
                return

            if confirm_resp.status_code == 200:
                _set_status_with_event(
                    transaction_id=transaction_id,
                    to_status=Transaction.Status.COMPLETED,
                    expected_status=Transaction.Status.RECOVERY_IN_PROGRESS,
                    actor=SagaEvent.ACTOR_RECOVERY,
                    http_status_code=payment_resp.status_code,
                    http_payload=payment_resp.json() if payment_resp.status_code == 200 else None,
                )
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  -> {transaction_id}: inventory confirmed, rolled forward to COMPLETED."
                    )
                )
                return
            elif confirm_resp.status_code >= 500:
                # 5xx Server error: not definitive, do NOT compensate! Revert claim so next sweep retries.
                _set_status_with_event(
                    transaction_id=transaction_id,
                    to_status=Transaction.Status.PAYMENT_PROCESSING,
                    expected_status=Transaction.Status.RECOVERY_IN_PROGRESS,
                    actor=SagaEvent.ACTOR_RECOVERY,
                    http_status_code=confirm_resp.status_code,
                    http_payload=confirm_resp.json() if confirm_resp.content else None,
                )
                self.stdout.write(
                    self.style.WARNING(
                        f"  -> {transaction_id}: inventory confirm returned 5xx ({confirm_resp.status_code}); claim reverted to PAYMENT_PROCESSING."
                    )
                )
                return
            else:
                # Definitive 4xx error (hold expired, seats not found)
                confirm_payload = confirm_resp.json() if confirm_resp.content else {}
                released_reason = confirm_payload.get("released_reason") if isinstance(confirm_payload, dict) else None
                if not released_reason:
                    try:
                        chk_resp = requests.get(
                            INVENTORY_CHECK_URL,
                            params={"transaction_id": str(transaction_id)},
                            headers=headers,
                            timeout=REQUEST_TIMEOUT,
                        )
                        if chk_resp.status_code == 200:
                            chk_data = chk_resp.json() if chk_resp.content else {}
                            released_reason = chk_data.get("released_reason")
                    except Exception:
                        pass

                if released_reason == "ADMIN_RESET":
                    logger.critical(
                        f"CRITICAL: Transaction {transaction_id} was released by ADMIN_RESET while payment is CHARGED! Auto-compensation/refund prohibited; manual review required."
                    )
                    _set_status_with_event(
                        transaction_id=transaction_id,
                        to_status=Transaction.Status.COMPENSATION_FAILED,
                        expected_status=Transaction.Status.RECOVERY_IN_PROGRESS,
                        actor=SagaEvent.ACTOR_RECOVERY,
                        http_status_code=confirm_resp.status_code,
                        http_payload={
                            "error": "Inventory was released by ADMIN_RESET while payment is CHARGED; auto-refund prohibited. Manual review required.",
                            "manual_review_required": True,
                            "released_reason": "ADMIN_RESET",
                            "payment_status": "CHARGED",
                        },
                    )
                    self.stdout.write(
                        self.style.ERROR(
                            f"  -> {transaction_id}: inventory released by ADMIN_RESET while payment CHARGED; auto-refund blocked, marked COMPENSATION_FAILED (manual_review_required)."
                        )
                    )
                    return

                # Otherwise (e.g. hold expired naturally) -> route to compensation
                self.stdout.write(
                    self.style.WARNING(
                        f"  -> {transaction_id}: inventory confirm failed definitively (HTTP {confirm_resp.status_code}); routing to compensation."
                    )
                )
                self._compensate(transaction_id, payment_was_charged=True)
                return

        if payment_status != PAYMENT_NOT_CHARGED:
            self._leave_in_progress(
                transaction_id, f"unrecognized payment status ({payment_status!r})"
            )
            return

        try:
            inventory_resp = requests.get(
                INVENTORY_CHECK_URL,
                params={"transaction_id": str(transaction_id)},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException:
            self._leave_in_progress(transaction_id, "inventory check failed")
            return

        inv_data = inventory_resp.json() if inventory_resp.status_code == 200 and inventory_resp.content else {}
        inventory_status = inv_data.get("status") if inventory_resp.status_code == 200 else None

        if inventory_status in (INVENTORY_RESERVED, "RESERVED"):
            self._compensate(transaction_id, payment_was_charged=False)
        elif inventory_status in (INVENTORY_RELEASED, INVENTORY_NOT_RESERVED, "RELEASED"):
            # Ensure payment is voided so an in-flight charge cannot land after marking TIMEOUT
            try:
                void_resp = requests.post(
                    PAYMENT_VOID_URL,
                    json={"transaction_id": str(transaction_id)},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
                if void_resp.status_code == 200:
                    v_data = void_resp.json()
                    if v_data.get("status") == "CHARGED":
                        if inv_data.get("released_reason") == "ADMIN_RESET":
                            logger.critical(
                                f"CRITICAL: Transaction {transaction_id} was released by ADMIN_RESET while payment is CHARGED! Auto-compensation/refund prohibited; manual review required."
                            )
                            _set_status_with_event(
                                transaction_id=transaction_id,
                                to_status=Transaction.Status.COMPENSATION_FAILED,
                                expected_status=Transaction.Status.RECOVERY_IN_PROGRESS,
                                actor=SagaEvent.ACTOR_RECOVERY,
                                http_status_code=void_resp.status_code,
                                http_payload={
                                    "error": "Inventory was released by ADMIN_RESET while payment is CHARGED; auto-refund prohibited. Manual review required.",
                                    "manual_review_required": True,
                                    "released_reason": "ADMIN_RESET",
                                    "payment_status": "CHARGED",
                                },
                            )
                            self.stdout.write(
                                self.style.ERROR(
                                    f"  -> {transaction_id}: inventory released by ADMIN_RESET while payment CHARGED; auto-refund blocked, marked COMPENSATION_FAILED (manual_review_required)."
                                )
                            )
                            return

                        self.stdout.write(
                            self.style.WARNING(
                                f"  -> {transaction_id}: payment was charged before void; routing to refund."
                            )
                        )
                        self._compensate(transaction_id, payment_was_charged=True)
                        return
            except requests.exceptions.RequestException:
                pass

            _set_status_with_event(
                transaction_id=transaction_id,
                to_status=Transaction.Status.TIMEOUT,
                expected_status=Transaction.Status.RECOVERY_IN_PROGRESS,
                actor=SagaEvent.ACTOR_RECOVERY,
                http_status_code=inventory_resp.status_code,
                http_payload=inventory_resp.json() if inventory_resp.status_code == 200 else None,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"  -> {transaction_id}: nothing reserved or charged, marked TIMEOUT."
                )
            )
        else:

            self._leave_in_progress(
                transaction_id, f"unrecognized inventory status ({inventory_status!r})"
            )

    def _leave_in_progress(self, transaction_id, reason):
        self.stdout.write(
            self.style.WARNING(
                f"  -> {transaction_id}: {reason}. "
                "Left as RECOVERY_IN_PROGRESS; will retry next sweep."
            )
        )

    def _compensate(self, transaction_id, payment_was_charged=None):
        outcome = execute_compensation_sweep(
            transaction_id=transaction_id,
            actor=SagaEvent.ACTOR_RECOVERY,
            original_status=Transaction.Status.COMPENSATION_FAILED,
            get_func=requests.get,
            post_func=requests.post,
        )
        if outcome.get("success"):
            self.stdout.write(
                self.style.SUCCESS(
                    f"  -> {transaction_id}: compensated successfully."
                )
            )
        else:
            self.stdout.write(
                self.style.ERROR(
                    f"  -> {transaction_id}: compensation partially failed; will retry next sweep."
                )
            )
