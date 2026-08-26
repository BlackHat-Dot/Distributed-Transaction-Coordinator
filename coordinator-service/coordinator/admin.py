from django.contrib import admin
from .models import Transaction, SagaEvent


class SagaEventInline(admin.TabularInline):
    model = SagaEvent
    extra = 0
    readonly_fields = ("seq", "from_status", "to_status", "actor", "http_status_code", "created_at")
    can_delete = False


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_id", "status", "seat_id", "amount", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("transaction_id", "seat_id")
    readonly_fields = ("created_at", "updated_at")
    inlines = [SagaEventInline]

    fieldsets = (
        ("Saga Transaction Overview", {
            "fields": ("transaction_id", "status", "amount", "created_at", "updated_at")
        }),
        ("Seat Booking Details", {
            "fields": ("seat_id",),
            "description": "Discrete physical seat identifier in Inventory Service (PostgreSQL Primary Key)."
        }),
        ("Legacy DTC Fields (Deprecated)", {
            "classes": ("collapse",),
            "fields": ("product_id", "quantity"),
            "description": "Retained for backward compatibility with older generic e-commerce payloads."
        }),
    )