from django.conf import settings
from django.urls import path
from payments.views import charge_payment, payment_check, refund_payment, void_payment, health_check

urlpatterns = [
    path('health/', health_check),
    path("payments/charge/", charge_payment),
    path('payments/check/', payment_check),
    path('payments/refund/', refund_payment),
    path('payments/void/', void_payment),
]

# Only mount /admin/ in local development mode (DEBUG=True)
if settings.DEBUG:
    from django.contrib import admin
    urlpatterns.insert(0, path('admin/', admin.site.urls))
