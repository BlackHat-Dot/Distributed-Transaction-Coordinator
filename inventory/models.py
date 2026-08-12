from django.db import models

class Product(models.Model):
    name = models.CharField(max_length=200)
    stock = models.PositiveIntegerField(default=0)
    # stock cannot be negative

    def __str__(self):
        return self.name

    # __str__ : used when print(Product)
    # , str(Product), product name in django server 
    # is also because of this __str__


class Reservation(models.Model):

    class Status(models.TextChoices):
        RESERVED = "RESERVED"
        RELEASED = "RELEASED"
        NOT_RESERVED = "NOT_RESERVED"

    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=0)
    transaction_id = models.UUIDField(unique=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RESERVED,
    )

    def __str__(self):
        return str(self.transaction_id)