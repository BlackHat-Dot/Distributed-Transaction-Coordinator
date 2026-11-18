from django.contrib import admin
from django.db.models import IntegerField
# Imports Django database Cast function to convert char-based seat numbers to integers for natural numerical sorting in the admin dashboard.
from django.db.models.functions import Cast
from .models import Event, Seat, Reservation


# Registers Event model with administrative interface to manage venue bookings, event start schedules, and seating layouts.
@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("name", "venue", "starts_at", "created_at")
    search_fields = ("name", "venue")


# Registers Seat model to inspect real-time seat availability, hold statuses, pricing, and active transaction leases.
@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "row", "seat_number", "price", "status", "held_by_transaction_id", "held_until")
    list_filter = ("status", "row", "event")
    search_fields = ("row", "seat_number", "held_by_transaction_id")

    def get_queryset(self, request):
        # Annotates querysets with integer-cast seat numbers so seats sort naturally (1, 2, 10) instead of alphabetically (1, 10, 2).
        qs = self.model._default_manager.get_queryset().annotate(
            seat_num_int=Cast("seat_number", IntegerField())
        )
        ordering = self.get_ordering(request)
        if ordering:
            qs = qs.order_by(*ordering)
        return qs

    # Enforces consistent primary ordering across venue events, seating rows, and natural numeric seat positions.
    def get_ordering(self, request):
        return ["event", "row", "seat_num_int"]


# Registers Reservation journal to provide operators an immutable audit trail of seat hold leases and saga confirmation states.
@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ("transaction_id", "seat", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("transaction_id",)