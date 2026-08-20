import requests
from datetime import timedelta
from django.utils import timezone
from django.core.management.base import BaseCommand
from coordinator.models import Transaction

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
            claimed = Transaction.objects.filter(
                transaction_id=transaction_id,
                status=from_status,
                updated_at__lt=threshold,
            ).update(
                status=Transaction.Status.RECOVERY_IN_PROGRESS,
                updated_at=timezone.now(),
            )

            if claimed != 1:
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

            Transaction.objects.filter(
                transaction_id=transaction_id,
                status=Transaction.Status.RECOVERY_IN_PROGRESS,
            ).update(
                status=Transaction.Status.COMPLETED,
                updated_at=timezone.now(),
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
            Transaction.objects.filter(
                transaction_id=transaction_id,
                status=Transaction.Status.RECOVERY_IN_PROGRESS,
            ).update(
                status=Transaction.Status.TIMEOUT,
                updated_at=timezone.now(),
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
        Transaction.objects.filter(
            transaction_id=transaction_id,
            status=Transaction.Status.RECOVERY_IN_PROGRESS,
        ).update(
            status=Transaction.Status.COMPENSATING,
            updated_at=timezone.now(),
        )

        try:
            resp = requests.post(
                INVENTORY_RELEASE_URL,
                json={"transaction_id": str(transaction_id)},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException:
            Transaction.objects.filter(
                transaction_id=transaction_id,
                status=Transaction.Status.COMPENSATING,
            ).update(
                status=Transaction.Status.COMPENSATION_FAILED,
                updated_at=timezone.now(),
            )
            self.stdout.write(
                self.style.ERROR(
                    f"  -> {transaction_id}: compensation failed (network error)."
                )
            )
            return

        if resp.status_code == 200:
            Transaction.objects.filter(
                transaction_id=transaction_id,
                status=Transaction.Status.COMPENSATING,
            ).update(
                status=Transaction.Status.COMPENSATED,
                updated_at=timezone.now(),
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"  -> {transaction_id}: compensated successfully."
                )
            )
        else:
            Transaction.objects.filter(
                transaction_id=transaction_id,
                status=Transaction.Status.COMPENSATING,
            ).update(
                status=Transaction.Status.COMPENSATION_FAILED,
                updated_at=timezone.now(),
            )
            self.stdout.write(
                self.style.ERROR(
                    f"  -> {transaction_id}: compensation rejected by inventory "
                    f"(HTTP {resp.status_code})."
                )
            )