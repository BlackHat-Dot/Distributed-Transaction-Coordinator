from django.contrib import admin
from django.urls import path
from payments.views import charge_payment, payment_check, refund_payment, health_check

urlpatterns = [
    path('admin/', admin.site.urls),
    path('health/', health_check),
    path("payments/charge/", charge_payment),
    path('payments/check/', payment_check),
    path('payments/refund/', refund_payment),
]

