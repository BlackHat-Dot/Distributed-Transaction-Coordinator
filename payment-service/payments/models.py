from django.db import models


# Represents an immutable financial transaction ledger tracking charges, refunds, and saga compensation voids.
class Payment(models.Model):

    # Defines the state machine lifecycle for payment records: CHARGED (captured), NOT_CHARGED, REFUNDED, or VOIDED.
    class Status(models.TextChoices):
        CHARGED = "CHARGED"
        NOT_CHARGED = "NOT_CHARGED"
        REFUNDED = "REFUNDED"
        VOIDED = "VOIDED"

    # Enforces strict global uniqueness on saga transaction UUIDs to guarantee payment idempotency at the database engine level.
    transaction_id = models.UUIDField(unique=True)
    user_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    user_email = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    # Stores financial transaction amounts with fixed two-decimal precision to prevent floating-point rounding discrepancies.
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    refunded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status="VOIDED") | models.Q(amount__isnull=False),
                name="amount_not_null_unless_voided",
            ),
        ]

    def __str__(self):
        return str(self.transaction_id)