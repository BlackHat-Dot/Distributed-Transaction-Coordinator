from django.contrib import admin
from .models import Transaction, SagaEvent


class SagaEventInline(admin.TabularInline):
    model = SagaEvent
    extra = 0
    readonly_fields = ("seq", "from_status", "to_status", "actor", "http_status_code", "created_at")
    can_delete = False


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_id", "status", "seat_numbers", "quantity", "amount", "updated_at")
    list_filter = ("status",)
    search_fields = ("transaction_id", "seat_numbers")
    readonly_fields = ("created_at", "updated_at")
    inlines = [SagaEventInline]

    fieldsets = (
        ("Saga Transaction Overview", {
            "fields": ("transaction_id", "status", "amount", "user_id", "user_email", "created_at", "updated_at")
        }),
        ("Seat Booking Details", {
            "fields": ("seat_ids", "seat_numbers", "quantity"),
            "description": "Discrete physical seat identifier(s) in Inventory Service."
        }),
    )