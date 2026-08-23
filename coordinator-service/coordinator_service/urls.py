from django.contrib import admin
from django.urls import path
from coordinator.views import (
    create_transaction,
    retry_compensation,
    get_transaction,
    list_transactions,
    list_transaction_events,
)


urlpatterns = [
    path('admin/', admin.site.urls),
    path("transactions/", create_transaction),
    path("transactions/<uuid:transaction_id>/retry-compensation/",
         retry_compensation),
    path("transactions/<uuid:transaction_id>/events/", list_transaction_events),
    path("transactions/<uuid:transaction_id>/", get_transaction),
    path("transactions/all/", list_transactions),
]
