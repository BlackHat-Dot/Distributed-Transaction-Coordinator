from django.contrib import admin
from django.urls import path
from coordinator.views import (
    create_transaction,
    retry_compensation,
    get_transaction,
    list_transactions,
    list_transaction_events,
    health_check,
    transactions_endpoint,
    trigger_recovery_sweep,
    recovery_status,
)
from coordinator.auth_views import (
    send_otp,
    verify_otp,
    get_me,
    heartbeat,
    logout,
)


urlpatterns = [
    path('admin/', admin.site.urls),
    path('health/', health_check),
    path('auth/send-otp/', send_otp),
    path('auth/verify-otp/', verify_otp),
    path('auth/me/', get_me),
    path('auth/heartbeat/', heartbeat),
    path('auth/logout/', logout),
    path("transactions/", transactions_endpoint),
    path("transactions/<uuid:transaction_id>/retry-compensation/",
         retry_compensation),
    path("transactions/<uuid:transaction_id>/events/", list_transaction_events),
    path("transactions/<uuid:transaction_id>/", get_transaction),
    path("transactions/all/", list_transactions),
    path("transactions/feed/", list_transactions),
    path("recovery/sweep/", trigger_recovery_sweep),
    path("recovery/status/", recovery_status),
]
