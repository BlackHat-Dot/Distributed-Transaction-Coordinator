from django.contrib import admin
from django.urls import path
from inventory.views import (
    product_list,
    reserve_product, 
    release_product,
    reservation_check,
    health_check
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('products/', product_list),
    path('products/reserve/', reserve_product),
    path('products/release/', release_product),
    path('reserve/check/', reservation_check),
    path('health/', health_check)
]
