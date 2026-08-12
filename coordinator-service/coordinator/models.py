from django.db import models


class Transaction(models.Model):

    class Status(models.TextChoices):
        STARTED = "STARTED"
        INVENTORY_RESERVED = "INVENTORY_RESERVED"
        INVENTORY_RESERVE_FAILED = "INVENTORY_RESERVE_FAILED"
        INVENTORY_TIMEOUT = "INVENTORY_TIMEOUT"
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
        COMPLETED = "COMPLETED"
        TIMEOUT = "TIMEOUT"
        CONNECTION_FAILED = "CONNECTION_FAILED"
        CHARGED = "CHARGED"

    transaction_id = models.UUIDField(unique=True)

    status = models.CharField(
        max_length=40,
        choices=Status.choices,
    )

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