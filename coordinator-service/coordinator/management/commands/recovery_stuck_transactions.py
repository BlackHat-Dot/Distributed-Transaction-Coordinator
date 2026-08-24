import requests
from datetime import timedelta
from django.utils import timezone
from django.core.management.base import BaseCommand
from coordinator.models import Transaction, SagaEvent
from coordinator.saga_log import _set_status_with_event

PAYMENT_CHECK_URL = "http://127.0.0.1:8001/payments/check/"
INVENTORY_CHECK_URL = "http://127.0.0.1:8000/reserve/check/"
INVENTORY_RELEASE_URL = "http://127.0.0.1:8000/products/release/"
REQUEST_TIMEOUT = 5


PAYMENT_CHARGED = "CHARGED"
PAYMENT_NOT_CHARGED = "NOT_CHARGED"

INVENTORY_RESERVED = Transaction.Status.INVENTORY_RESERVED
INVENTORY_RELEASED = Transaction.Status.INVENTORY_RELEASED
INVENTORY_NOT_RESERVED = Transaction.Status.NOT_RESERVED



class Command(BaseCommand):
    help = "Recovers transactions stuck in non-terminal states due to coordinator crashes."

    def handle(self, *args, **options):
        threshold = timezone.now() - timedelta(minutes=5)

        self.recover_payment_processing(threshold)
        self.recover_reserved(threshold)
        self.recover_started(threshold)
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

        try:
            payment_resp = requests.get(
                PAYMENT_CHECK_URL,
                params={"transaction_id": str(transaction_id)},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException:
            self._leave_in_progress(transaction_id, "payment check failed")
            return

        payment_status = payment_resp.json().get("status") if payment_resp.status_code == 200 else None

        if payment_status == PAYMENT_CHARGED:

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
                    f"  -> {transaction_id}: rolled forward to COMPLETED."
                )
            )
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
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException:
            self._leave_in_progress(transaction_id, "inventory check failed")
            return

        inventory_status = inventory_resp.json().get("status") if inventory_resp.status_code == 200 else None

        if inventory_status == INVENTORY_RESERVED:
            self._compensate(transaction_id)
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

    def _compensate(self, transaction_id):
        _set_status_with_event(
            transaction_id=transaction_id,
            to_status=Transaction.Status.COMPENSATING,
            expected_status=Transaction.Status.RECOVERY_IN_PROGRESS,
            actor=SagaEvent.ACTOR_RECOVERY,
            http_status_code=None,
            http_payload=None,
        )

        try:
            resp = requests.post(
                INVENTORY_RELEASE_URL,
                json={"transaction_id": str(transaction_id)},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException:
            _set_status_with_event(
                transaction_id=transaction_id,
                to_status=Transaction.Status.COMPENSATION_FAILED,
                expected_status=Transaction.Status.COMPENSATING,
                actor=SagaEvent.ACTOR_RECOVERY,
                http_status_code=None,
                http_payload=None,
            )
            self.stdout.write(
                self.style.ERROR(
                    f"  -> {transaction_id}: compensation failed (network error)."
                )
            )
            return

        if resp.status_code == 200:
            _set_status_with_event(
                transaction_id=transaction_id,
                to_status=Transaction.Status.COMPENSATED,
                expected_status=Transaction.Status.COMPENSATING,
                actor=SagaEvent.ACTOR_RECOVERY,
                http_status_code=resp.status_code,
                http_payload=resp.json() if resp.status_code == 200 else None,
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
                http_status_code=resp.status_code,
                http_payload=resp.json() if resp.status_code == 200 else None,
            )
            self.stdout.write(
                self.style.ERROR(
                    f"  -> {transaction_id}: compensation rejected by inventory "
                    f"(HTTP {resp.status_code})."
                )
            )