from django.db import models


class Payment(models.Model):

    class Status(models.TextChoices):
        CHARGED = "CHARGED"

    transaction_id = models.UUIDField(unique=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return str(self.transaction_id)