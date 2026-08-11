from django.db import models

class Transaction(models.Model):
    transaction_id = models.UUIDField(unique=True)
    status = models.CharField(max_length=30)

    product_id = models.IntegerField(null=True)
    quantity = models.IntegerField(null=True)

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )

    created_at = models.DateTimeField(auto_now_add=True) # one time register , no change for each status update
    updated_at = models.DateTimeField(auto_now=True) # timestamp update for each change in record

    def __str__(self):
        return str(self.transaction_id)