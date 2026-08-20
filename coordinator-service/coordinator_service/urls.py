from django.contrib import admin
from django.urls import path
from coordinator.views import (
    create_transaction,
    retry_compensation,
    get_transaction,
    list_transactions,
)


urlpatterns = [
    path('admin/', admin.site.urls),
    path("transactions/", create_transaction),
    path("transactions/<uuid:transaction_id>/retry-compensation/",
         retry_compensation),
    path("transactions/<uuid:transaction_id>/",get_transaction),
    path("transactions/all/",list_transactions),
]
