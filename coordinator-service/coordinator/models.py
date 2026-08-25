from django.db import models


class Transaction(models.Model):

    class Status(models.TextChoices):
        STARTED = "STARTED"
        INVENTORY_RESERVED = "INVENTORY_RESERVED"
        INVENTORY_RELEASED = "INVENTORY_RELEASED"
        INVENTORY_RESERVE_FAILED = "INVENTORY_RESERVE_FAILED"
        INVENTORY_TIMEOUT = "INVENTORY_TIMEOUT"
        NOT_RESERVED = "NOT_RESERVED"
        INVENTORY_CONNECTION_ERROR = "INVENTORY_CONNECTION_ERROR"
        PAYMENT_PROCESSING = "PAYMENT_PROCESSING"
        PAYMENT_FAILED = "PAYMENT_FAILED"
        PAYMENT_TIMEOUT = "PAYMENT_TIMEOUT"
        PAYMENT_CONNECTION_ERROR = "PAYMENT_CONNECTION_ERROR"
        COMPENSATING = "COMPENSATING"
        COMPENSATED = "COMPENSATED"
        COMPENSATION_FAILED = "COMPENSATION_FAILED"
        COMPENSATED_ON_RETRY = "COMPENSATED_ON_RETRY"
        RETRY_COMPENSATION_FAILED = "RETRY_COMPENSATION_FAILED"
        RECOVERY_IN_PROGRESS = "RECOVERY_IN_PROGRESS"
        COMPLETED = "COMPLETED"
        CONNECTION_FAILED = "CONNECTION_FAILED"
        CHARGED = "CHARGED"
        TIMEOUT = "TIMEOUT"

    transaction_id = models.UUIDField(unique=True)

    status = models.CharField(
        max_length=40,
        choices=Status.choices,
    )

    seat_id = models.IntegerField(null=True)
    product_id = models.IntegerField(null=True)
    quantity = models.IntegerField(null=True)

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return str(self.transaction_id)


class SagaEvent(models.Model):

    ACTOR_REQUEST = "request"
    ACTOR_RECOVERY = "recovery"
    ACTOR_RETRY = "retry"

    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.CASCADE,
        related_name="events",
    )
    seq = models.PositiveIntegerField(
        help_text="1-based sequence number, monotonically increasing per transaction.",
    )
    from_status = models.CharField(max_length=40)
    to_status = models.CharField(max_length=40)
    actor = models.CharField(
        max_length=20,
        help_text="Which code path wrote this transition: 'request', 'recovery', or 'retry'.",
    )
    http_status_code = models.IntegerField(null=True, blank=True)
    http_payload = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("transaction", "seq")]
        ordering = ["transaction", "seq"]

    def __str__(self):
        return (
            f"SagaEvent(txn={self.transaction_id}, seq={self.seq}, "
            f"{self.from_status}->{self.to_status})"
        )