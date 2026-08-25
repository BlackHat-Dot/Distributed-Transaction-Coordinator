from django.contrib import admin
from django.urls import path
from inventory.views import (
    event_list,
    seat_list,
    reserve_seat,
    confirm_seat,
    release_seat,
    reservation_check,
    sweep_expired_holds,
    reset_seats,
    health_check,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('events/', event_list),
    path('seats/', seat_list),
    path('seats/reserve/', reserve_seat),
    path('seats/confirm/', confirm_seat),
    path('seats/release/', release_seat),
    path('seats/check/', reservation_check),
    path('seats/sweep-expired/', sweep_expired_holds),
    path('seats/reset/', reset_seats),
    path('reserve/check/', reservation_check),
    path('health/', health_check),
]
