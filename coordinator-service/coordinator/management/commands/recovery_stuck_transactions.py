import requests
from datetime import timedelta
from django.utils import timezone
from django.core.management.base import BaseCommand
from coordinator.models import Transaction

class Command(BaseCommand):
    help = "Recovers transactions stuck in non-terminal states due to coordinator crashes."

    def handle(self, *args, **options):
        # Time threshold: 5 minutes without an update means it's orphaned.
        threshold = timezone.now() - timedelta(minutes=5)
        # if updated_at < threshold then the transaction is considered stale.
        
        self.recover_reserved(threshold)
        self.recover_payment_processing(threshold)
        self.recover_started(threshold)
        
        self.stdout.write(self.style.SUCCESS("Recovery sweep complete."))

    def recover_reserved(self, threshold):
        """Stuck after Inventory, before Payment. We must compensate."""
        stuck_reserved = Transaction.objects.filter(
            status=Transaction.Status.INVENTORY_RESERVED,
            updated_at__lt=threshold
        )
        for txn in stuck_reserved:
            self.stdout.write(f"Recovering {txn.transaction_id} stuck in INVENTORY_RESERVED...")
            
            # Move to compensating
            txn.status = Transaction.Status.COMPENSATING
            txn.save(update_fields=['status'])

            # Trigger release
            self._release_inventory(txn)
            

    def recover_payment_processing(self, threshold):
        """Stuck waiting for Payment. We must check outcome first."""
        stuck_payment = Transaction.objects.filter(
            status=Transaction.Status.PAYMENT_PROCESSING,
            updated_at__lt=threshold
        )
        for txn in stuck_payment:
            self.stdout.write(f"Recovering {txn.transaction_id} stuck in PAYMENT_PROCESSING...")
            
            try:
                # Check payment status
                check_resp = requests.get(
                    "http://127.0.0.1:8001/payments/check/",
                    params={"transaction_id": str(txn.transaction_id)},
                    timeout=5
                )
                if check_resp.status_code == 200 and check_resp.json().get("status") == "CHARGED":
                    # Roll forward! Payment succeeded, we just crashed before saving.
                    txn.status = Transaction.Status.COMPLETED
                    txn.save(update_fields=['status'])
                    self.stdout.write(self.style.SUCCESS(f"  -> Rolled forward to COMPLETED"))
                    continue
            except requests.exceptions.RequestException:
                self.stdout.write(self.style.WARNING("  -> Payment check failed. Will retry next sweep."))
                continue # Skip compensation if we can't reliably determine payment state

            # If not charged, we must roll backward
            txn.status = Transaction.Status.COMPENSATING
            txn.save(update_fields=['status'])
            self._release_inventory(txn)

    def recover_started(self, threshold):
        """Stuck before reaching Inventory. Just mark as TIMEOUT."""
        updated_count = Transaction.objects.filter(
            status=Transaction.Status.STARTED,
            updated_at__lt=threshold
        ).update(status=Transaction.Status.TIMEOUT)
        
        if updated_count > 0:
            self.stdout.write(f"Marked {updated_count} STARTED transactions as TIMEOUT.")

    def _release_inventory(self, txn):
        """Helper to call inventory release."""
        try:
            resp = requests.post(
                "http://127.0.0.1:8000/products/release/",
                json={"transaction_id": str(txn.transaction_id)},
                timeout=5
            )
            if resp.status_code == 200:
                txn.status = Transaction.Status.COMPENSATED
                self.stdout.write(self.style.SUCCESS(f"  -> Compensated successfully"))
            else:
                txn.status = Transaction.Status.COMPENSATION_FAILED
                self.stdout.write(self.style.ERROR(f"  -> Compensation rejected by inventory"))
        except requests.exceptions.RequestException:
            txn.status = Transaction.Status.COMPENSATION_FAILED
            self.stdout.write(self.style.ERROR(f"  -> Compensation failed due to network error"))
            
        txn.save(update_fields=['status', 'updated_at'])