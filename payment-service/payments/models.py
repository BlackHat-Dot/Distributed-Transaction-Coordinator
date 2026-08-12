from django.db import models

class Payment(models.Model):
    transaction_id = models.UUIDField(unique=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default="PENDING")

    def __str__(self):
        return self.transaction_id