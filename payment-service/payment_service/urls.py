from django.contrib import admin
from django.urls import path
from payments.views import charge_payment, payment_check

urlpatterns = [
    path('admin/', admin.site.urls),
    path("payments/charge/", charge_payment),
    path('payments/check/', payment_check)
]
