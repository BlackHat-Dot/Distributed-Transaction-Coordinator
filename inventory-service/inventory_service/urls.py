from django.conf import settings
from django.urls import path
from inventory.views import (
    event_list,
    seat_list,
    seat_pricing,
    reserve_seat,
    confirm_seat,
    release_seat,
    check_reservation,
    reset_seats,
    sweep_expired_holds,
    health_check,
)

urlpatterns = [
    path('events/', event_list),
    path('seats/', seat_list),
    path('seats/pricing/', seat_pricing),
    path('seats/reserve/', reserve_seat),
    path('seats/confirm/', confirm_seat),
    path('seats/release/', release_seat),
    path('seats/check/', check_reservation),
    path('seats/sweep-expired/', sweep_expired_holds),
    path('seats/reset/', reset_seats),
    path('reserve/check/', check_reservation),
    path('health/', health_check),
]

# Only mount /admin/ in local development mode (DEBUG=True)
if settings.DEBUG:
    from django.contrib import admin
    urlpatterns.insert(0, path('admin/', admin.site.urls))
