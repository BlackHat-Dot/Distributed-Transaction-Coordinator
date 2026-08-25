from django.contrib import admin
from .models import Event, Seat, Reservation

admin.site.register(Event)
admin.site.register(Seat)
admin.site.register(Reservation)