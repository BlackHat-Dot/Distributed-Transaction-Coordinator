import requests
from datetime import timedelta
from django.utils import timezone
from django.core.management.base import BaseCommand
from coordinator.models import Transaction

# ---------------------------------------------------------------------------
# Why we pass updated_at=timezone.now() to every .update() call
# ---------------------------------------------------------------------------
# Django's auto_now=True on updated_at only fires on model.save().
# QuerySet.update() bypasses auto_now entirely, so without the explicit
# assignment the column would remain stale.  Keeping updated_at fresh on every
# state transition is what allows the reclaim mechanism to work: a later sweep
# can tell that a row is genuinely stuck in RECOVERY_IN_PROGRESS (worker
# crashed) because its updated_at has aged past the threshold again.
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    help = "Recovers transactions stuck in non-terminal states due to coordinator crashes."

    def handle(self, *args, **options):
        # Transactions not updated in the last 5 minutes are considered stale.
        threshold = timezone.now() - timedelta(minutes=5)

        self.recover_payment_processing(threshold)
        self.recover_reserved(threshold)
        self.recover_started(threshold)
        # Must run LAST: reclaims rows a previous sweep claimed but then
        # crashed on before completing.  Running it last avoids a false
        # positive where a row was just claimed by an earlier step in
        # this same sweep and its updated_at is therefore fresh.
        self.recover_in_progress(threshold)

        self.stdout.write(self.style.SUCCESS("Recovery sweep complete."))

    # -----------------------------------------------------------------------
    # PAYMENT_PROCESSING
    # -----------------------------------------------------------------------
    def recover_payment_processing(self, threshold):
        """
        Stuck after inventory was reserved, during/after the payment charge.

        We MUST check the Payment service first because the charge may have
        succeeded even though the Coordinator crashed before writing COMPLETED.

        Claim atomically → check Payment (HTTP, outside any DB txn) →
        roll forward (COMPLETED) or roll backward (COMPENSATING → release).
        """
        candidates = list(
            Transaction.objects.filter(
                status=Transaction.Status.PAYMENT_PROCESSING,
                updated_at__lt=threshold,
            ).values_list('transaction_id', flat=True)
        )

        for transaction_id in candidates:
            # -----------------------------------------------------------------
            # ATOMIC CLAIM
            # Postgres executes this as a single UPDATE … WHERE statement with
            # a row-level lock.  Exactly one concurrent actor will match the
            # WHERE predicate and receive claimed=1; all others get 0 and skip.
            #
            # We also refresh updated_at so that IF this worker crashes
            # mid-recovery, the recover_in_progress() sweep can detect and
            # reclaim the row after another threshold period has elapsed.
            # -----------------------------------------------------------------
            claimed = Transaction.objects.filter(
                transaction_id=transaction_id,
                status=Transaction.Status.PAYMENT_PROCESSING,
                updated_at__lt=threshold,
            ).update(
                status=Transaction.Status.RECOVERY_IN_PROGRESS,
                updated_at=timezone.now(),
            )

            if claimed != 1:
                self.stdout.write(
                    f"  PAYMENT_PROCESSING {transaction_id}: already claimed, skipping."
                )
                continue

            self.stdout.write(
                f"Recovering {transaction_id} claimed from PAYMENT_PROCESSING..."
            )
            self._resolve_via_payment_check(transaction_id)

    # -----------------------------------------------------------------------
    # INVENTORY_RESERVED
    # -----------------------------------------------------------------------
    def recover_reserved(self, threshold):
        """
        Stuck after inventory was reserved but before payment was ever
        attempted.  Always compensate (release inventory).
        """
        candidates = list(
            Transaction.objects.filter(
                status=Transaction.Status.INVENTORY_RESERVED,
                updated_at__lt=threshold,
            ).values_list('transaction_id', flat=True)
        )

        for transaction_id in candidates:
            claimed = Transaction.objects.filter(
                transaction_id=transaction_id,
                status=Transaction.Status.INVENTORY_RESERVED,
                updated_at__lt=threshold,
            ).update(
                status=Transaction.Status.RECOVERY_IN_PROGRESS,
                updated_at=timezone.now(),
            )

            if claimed != 1:
                self.stdout.write(
                    f"  INVENTORY_RESERVED {transaction_id}: already claimed, skipping."
                )
                continue

            self.stdout.write(
                f"Recovering {transaction_id} claimed from INVENTORY_RESERVED..."
            )
            self._compensate(transaction_id)

    # -----------------------------------------------------------------------
    # STARTED
    # -----------------------------------------------------------------------
    def recover_started(self, threshold):
        """
        Stuck before ever reaching Inventory.  No distributed state to undo —
        mark TIMEOUT atomically.

        This is a bulk update because:
          - No HTTP call is required, so there is no work to "own."
          - The single UPDATE is simultaneously the claim AND the final write.
          - If two workers run concurrently, the second will match 0 rows
            (the first already changed status away from STARTED) — safe.
        """
        updated_count = Transaction.objects.filter(
            status=Transaction.Status.STARTED,
            updated_at__lt=threshold,
        ).update(
            status=Transaction.Status.TIMEOUT,
            updated_at=timezone.now(),
        )

        if updated_count > 0:
            self.stdout.write(
                f"Marked {updated_count} STARTED transaction(s) as TIMEOUT."
            )

    # -----------------------------------------------------------------------
    # RECOVERY_IN_PROGRESS  (reclaim of crashed recovery workers)
    # -----------------------------------------------------------------------
    def recover_in_progress(self, threshold):
        """
        Reclaims transactions that a previous recovery sweep left in
        RECOVERY_IN_PROGRESS because the worker crashed after the atomic claim
        but before it could write the final status.

        How the reclaim detects a crashed worker
        -----------------------------------------
        When a recovery worker claims a row it sets updated_at=timezone.now().
        If the worker then crashes without writing a terminal status, updated_at
        ages normally.  Once it again falls below `threshold` a new sweep can
        re-enter this method, reclaim the row (bumping updated_at again), and
        retry the recovery logic.

        Mutual exclusion between concurrent reclaim attempts
        -----------------------------------------------------
        The WHERE clause includes updated_at__lt=threshold.  The winning actor
        sets updated_at=now() as part of its UPDATE.  Any subsequent actor
        evaluating the same row will find updated_at >= threshold and match
        0 rows (claimed=0), so it skips — no second actor proceeds.

        Which recovery path to follow
        ------------------------------
        We don't store which original stale state caused the claim, but we
        don't need to: checking the Payment service is safe for every case.
          - STARTED never reaches RECOVERY_IN_PROGRESS (bulk TIMEOUT step).
          - INVENTORY_RESERVED had no charge attempt → check returns NOT_CHARGED
            → compensate (correct).
          - PAYMENT_PROCESSING → check returns the real outcome (correct).
        """
        candidates = list(
            Transaction.objects.filter(
                status=Transaction.Status.RECOVERY_IN_PROGRESS,
                updated_at__lt=threshold,
            ).values_list('transaction_id', flat=True)
        )

        for transaction_id in candidates:
            # Reclaim: status value stays the same but updated_at is refreshed.
            # The updated_at__lt=threshold guard is what enforces mutual
            # exclusion between concurrent workers, exactly like the initial
            # claims above.
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
                f"(previous recovery worker likely crashed)..."
            )
            self._resolve_via_payment_check(transaction_id)

    # -----------------------------------------------------------------------
    # Shared helpers
    # -----------------------------------------------------------------------
    def _resolve_via_payment_check(self, transaction_id):
        """
        Check the Payment service and decide direction.
        Called from both recover_payment_processing() and recover_in_progress().
        The DB transaction from the claim .update() has already committed;
        no DB transaction is open while this HTTP call is in flight.
        """
        try:
            check_resp = requests.get(
                "http://127.0.0.1:8001/payments/check/",
                params={"transaction_id": str(transaction_id)},
                timeout=5,
            )
        except requests.exceptions.RequestException:
            # Cannot determine outcome.  Leave as RECOVERY_IN_PROGRESS so the
            # next sweep can retry (updated_at was already refreshed by the
            # claim, so the row won't be re-eligible until another threshold
            # period has elapsed — preventing a tight retry storm).
            self.stdout.write(
                self.style.WARNING(
                    f"  -> {transaction_id}: payment check failed. "
                    "Left as RECOVERY_IN_PROGRESS; will retry next sweep."
                )
            )
            return

        if (
            check_resp.status_code == 200
            and check_resp.json().get("status") == "CHARGED"
        ):
            # Roll FORWARD: payment succeeded.
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
        else:
            # Roll BACKWARD: not charged, release inventory.
            self._compensate(transaction_id)

    def _compensate(self, transaction_id):
        """
        RECOVERY_IN_PROGRESS → COMPENSATING (short commit) →
        release HTTP call →
        COMPENSATED or COMPENSATION_FAILED (short commit).

        No DB transaction is open across the HTTP call.
        Every .update() includes updated_at=timezone.now() explicitly because
        auto_now does not fire on queryset updates.
        Each transition is conditional on the current status to protect against
        concurrent actors that may have advanced the row independently.
        """
        # Short commit: mark that compensation is starting.
        Transaction.objects.filter(
            transaction_id=transaction_id,
            status=Transaction.Status.RECOVERY_IN_PROGRESS,
        ).update(
            status=Transaction.Status.COMPENSATING,
            updated_at=timezone.now(),
        )

        # HTTP call — no DB transaction open here.
        try:
            resp = requests.post(
                "http://127.0.0.1:8000/products/release/",
                json={"transaction_id": str(transaction_id)},
                timeout=5,
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
