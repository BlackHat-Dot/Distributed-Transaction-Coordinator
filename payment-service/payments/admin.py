from django.contrib import admin
# Imports the Payment ledger model to register it with the Django administrative interface.
from .models import Payment

# Registers Payment model with Django admin to allow operators to inspect financial ledger transactions and refund statuses.
admin.site.register(Payment)
