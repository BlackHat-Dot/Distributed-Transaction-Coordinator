from django.db import models

class Product(models.Model):
    name = models.CharField(max_length=200)
    stock = models.PositiveIntegerField(default=0)
    # stock cannot be negative

    def __str__(self):
        return self.name