import requests
from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from django.core.management.base import BaseCommand
from coordinator.models import Transaction, SagaEvent
from coordinator.saga_log import _set_status_with_event

import os

INVENTORY_BASE_URL = os.getenv("INVENTORY_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
PAYMENT_BASE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")

PAYMENT_CHECK_URL = f"{PAYMENT_BASE_URL}/payments/check/"
PAYMENT_REFUND_URL = f"{PAYMENT_BASE_URL}/payments/refund/"
INVENTORY_CHECK_URL = f"{INVENTORY_BASE_URL}/reserve/check/"
INVENTORY_CONFIRM_URL = f"{INVENTORY_BASE_URL}/seats/confirm/"
INVENTORY_RELEASE_URL = f"{INVENTORY_BASE_URL}/seats/release/"
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "5"))
INTERNAL_SERVICE_KEY = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY")


PAYMENT_CHARGED = "CHARGED"
PAYMENT_NOT_CHARGED = "NOT_CHARGED"

INVENTORY_RESERVED = Transaction.Status.INVENTORY_RESERVED
INVENTORY_RELEASED = Transaction.Status.INVENTORY_RELEASED
INVENTORY_NOT_RESERVED = Transaction.Status.NOT_RESERVED



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
        self.recover_started(threshold)
        self.recover_timeouts(threshold)
        self.recover_compensating(threshold)
        self.recover_compensation_failed(threshold)

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

            # Before completing, confirm inventory
            try:
                confirm_resp = requests.post(
                    INVENTORY_CONFIRM_URL,
                    json={"seat_ids": seat_ids, "transaction_id": str(transaction_id)},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.exceptions.RequestException:
                self._leave_in_progress(transaction_id, "inventory confirm network error during recovery")
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
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"  -> {transaction_id}: inventory confirm failed (HTTP {confirm_resp.status_code}); routing to compensation."
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

        inventory_status = inventory_resp.json().get("status") if inventory_resp.status_code == 200 else None

        if inventory_status == INVENTORY_RESERVED:
            self._compensate(transaction_id, payment_was_charged=False)
        elif inventory_status in (INVENTORY_RELEASED, INVENTORY_NOT_RESERVED):
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
        _set_status_with_event(
            transaction_id=transaction_id,
            to_status=Transaction.Status.COMPENSATING,
            expected_status=Transaction.Status.RECOVERY_IN_PROGRESS,
            actor=SagaEvent.ACTOR_RECOVERY,
            http_status_code=None,
            http_payload=None,
        )

        txn = Transaction.objects.filter(transaction_id=transaction_id).first()
        seat_ids = txn.seat_ids if txn else []

        headers = {
            "X-Correlation-ID": str(transaction_id),
            "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
        }
        # 1. Attempt Inventory Release
        release_ok = False
        release_status_code = None
        release_payload = None
        try:
            resp = requests.post(
                INVENTORY_RELEASE_URL,
                json={"transaction_id": str(transaction_id), "seat_ids": seat_ids},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            release_status_code = resp.status_code
            release_ok = (resp.status_code == 200)
            if release_ok:
                release_payload = resp.json() if resp.content else None
            else:
                self.stdout.write(
                    self.style.ERROR(
                        f"  -> {transaction_id}: compensation rejected by inventory (HTTP {resp.status_code})."
                    )
                )
        except requests.exceptions.RequestException:
            self.stdout.write(
                self.style.ERROR(
                    f"  -> {transaction_id}: compensation network error releasing inventory."
                )
            )

        # 2. Check if payment was charged and needs refund (regardless of release status)
        if payment_was_charged is None:
            try:
                p_resp = requests.get(
                    PAYMENT_CHECK_URL,
                    params={"transaction_id": str(transaction_id)},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
                if p_resp.status_code == 200 and p_resp.json().get("status") == PAYMENT_CHARGED:
                    payment_was_charged = True
            except requests.exceptions.RequestException:
                pass

        refund_ok = not payment_was_charged
        refund_status_code = None
        refund_payload = None
        if payment_was_charged:
            try:
                refund_resp = requests.post(
                    PAYMENT_REFUND_URL,
                    json={"transaction_id": str(transaction_id)},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
                refund_status_code = refund_resp.status_code
                refund_ok = (refund_resp.status_code == 200)
                if refund_ok:
                    refund_payload = refund_resp.json() if refund_resp.content else None
                else:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  -> {transaction_id}: refund rejected by payment service (HTTP {refund_resp.status_code})."
                        )
                    )
            except requests.exceptions.RequestException:
                self.stdout.write(
                    self.style.ERROR(
                        f"  -> {transaction_id}: refund failed (network error reaching payment service)."
                    )
                )

        # 3. Final State Resolution
        if release_ok and refund_ok:
            _set_status_with_event(
                transaction_id=transaction_id,
                to_status=Transaction.Status.COMPENSATED,
                expected_status=Transaction.Status.COMPENSATING,
                actor=SagaEvent.ACTOR_RECOVERY,
                http_status_code=release_status_code or 200,
                http_payload=release_payload,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"  -> {transaction_id}: compensated successfully."
                )
            )
        else:
            _set_status_with_event(
                transaction_id=transaction_id,
                to_status=Transaction.Status.COMPENSATION_FAILED,
                expected_status=Transaction.Status.COMPENSATING,
                actor=SagaEvent.ACTOR_RECOVERY,
                http_status_code=refund_status_code if not refund_ok else release_status_code,
                http_payload=refund_payload if not refund_ok else release_payload,
            )
            self.stdout.write(
                self.style.ERROR(
                    f"  -> {transaction_id}: compensation partially failed; will retry next sweep."
                )
            )