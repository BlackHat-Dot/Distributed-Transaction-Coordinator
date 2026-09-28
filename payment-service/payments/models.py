from django.db import models


class Payment(models.Model):

    class Status(models.TextChoices):
        CHARGED = "CHARGED"
        NOT_CHARGED = "NOT_CHARGED"
        REFUNDED = "REFUNDED"
        VOIDED = "VOIDED"

    transaction_id = models.UUIDField(unique=True)
    user_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    user_email = models.CharField(max_length=255, null=True, blank=True, db_index=True)
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