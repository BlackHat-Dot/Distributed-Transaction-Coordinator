from django.contrib import admin
from .models import Event, Seat, Reservation


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("name", "venue", "starts_at", "created_at")
    search_fields = ("name", "venue")


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "row", "seat_number", "price", "status", "held_by_transaction_id", "held_until")
    list_filter = ("status", "row", "event")
    search_fields = ("row", "seat_number", "held_by_transaction_id")
    ordering = ("event", "row", "seat_number")


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ("transaction_id", "seat", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("transaction_id",)