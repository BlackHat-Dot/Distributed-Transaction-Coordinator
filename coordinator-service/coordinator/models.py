from django.db import models


class Transaction(models.Model):

    class Status(models.TextChoices):
        STARTED = "STARTED"
        HOLDING = "HOLDING"
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

    TERMINAL_STATUSES = {
        Status.COMPLETED,
        Status.COMPENSATED,
        Status.COMPENSATION_FAILED,
        Status.INVENTORY_RESERVE_FAILED,
        Status.COMPENSATED_ON_RETRY,
        Status.RETRY_COMPENSATION_FAILED,
        Status.TIMEOUT,
        Status.PAYMENT_TIMEOUT,
        Status.PAYMENT_FAILED,
        Status.PAYMENT_CONNECTION_ERROR,
        Status.INVENTORY_TIMEOUT,
        Status.INVENTORY_CONNECTION_ERROR,
    }

    # Non-terminal states that qualify for automated saga recovery and compensation sweeps
    RECOVERABLE_STATUSES = {
        Status.PAYMENT_PROCESSING,
        Status.INVENTORY_RESERVED,
        Status.STARTED,
        Status.INVENTORY_TIMEOUT,
        Status.PAYMENT_TIMEOUT,
        Status.INVENTORY_CONNECTION_ERROR,
        Status.PAYMENT_CONNECTION_ERROR,
        Status.COMPENSATING,
        Status.COMPENSATION_FAILED,
        Status.RETRY_COMPENSATION_FAILED,
    }

    transaction_id = models.UUIDField(unique=True)
    user_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    user_email = models.CharField(max_length=255, null=True, blank=True, db_index=True)

    status = models.CharField(
        max_length=40,
        choices=Status.choices,
    )

    seat_ids = models.JSONField(null=True, blank=True)
    seat_numbers = models.CharField(max_length=255, null=True, blank=True)
    quantity = models.IntegerField(null=True, default=1)

    event_name = models.CharField(max_length=200, null=True, blank=True)
    venue = models.CharField(max_length=200, null=True, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )

    # auto_now_add : Value inserted at record creation and persist without a change
    # auto_now : Value inserted at record creation and changed if record is updated
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
        # Inner configuration class that provides database-level options and metadata for this Django model.

        unique_together = [("transaction", "seq")]
        # Enforces a composite UNIQUE database constraint: within a single transaction, no two events can have the same sequence number ('seq').

        ordering = ["transaction", "seq"]
        # Sets the default query sort order (SQL: ORDER BY transaction_id ASC, seq ASC), ensuring events are always retrieved in chronological sequence.

    def __str__(self):
        return (
            f"SagaEvent(txn={self.transaction_id}, seq={self.seq}, "
            f"{self.from_status}->{self.to_status})"
        )


class Ticket(models.Model):
    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.CASCADE,
        related_name="tickets",
    )
    seat_id = models.IntegerField(db_index=True)
    seat_label = models.CharField(max_length=64)
    event_name = models.CharField(max_length=200)
    venue = models.CharField(max_length=200, blank=True, default="")
    starts_at = models.DateTimeField(null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    ticket_code = models.CharField(max_length=64, unique=True, db_index=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True, db_index=True)
    used_by = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        unique_together = [("transaction", "seat_id")]
        ordering = ["seat_id"]

    def __str__(self):
        return f"Ticket({self.ticket_code}, txn={self.transaction_id}, seat={self.seat_label})"
