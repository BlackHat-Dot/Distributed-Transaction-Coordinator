from django.db import models

class Transaction(models.Model):
    transaction_id = models.UUIDField(unique=True)
    status = models.CharField(max_length=30)

    def __str__(self):
        return str(self.transaction_id)