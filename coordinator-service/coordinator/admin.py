# Imports Django's built-in administration module so you can register database models to a ready-to-use web management dashboard.
from django.contrib import admin

# Imports your core domain models—Transaction (the main state record) and SagaEvent (the step-by-step audit log)
from .models import Transaction, SagaEvent

# Defines a compact, row-by-row table layout in the Django admin panel to embed child SagaEvent records directly inside their parent Transaction detail page.
# admin.TabularInline -> A Django admin view helper that displays related child records horizontally as rows in a table on the parent model's edit page.
class SagaEventInline(admin.TabularInline):
    # Tells Django this table displays records from the SagaEvent database model.
    model = SagaEvent

    # Prevents Django from showing blank empty rows for creating new events at the bottom.
    extra = 0

    # Locks audit trail fields to view-only so admins cannot tamper with historical state transitions.
    readonly_fields = ("seq", "from_status", "to_status", "actor", "http_status_code", "created_at")

    # Disables the delete checkbox so critical audit events can never be removed from history.
    can_delete = False

# Registers the Transaction model with Django Admin using the customized interface defined below.
@admin.register(Transaction)
# Defines the custom dashboard controller and UI configuration for managing Transaction records.
class TransactionAdmin(admin.ModelAdmin):
    # Specifies which database columns appear as table headers on the main transaction listing page.
    list_display = ("transaction_id", "status", "seat_numbers", "quantity", "amount", "updated_at")

    # Adds a sidebar filter panel allowing admins to quickly isolate transactions by status (e.g., pending, completed).
    list_filter = ("status",)

    # Adds a top search bar to look up records by specific transaction IDs or seat numbers.
    search_fields = ("transaction_id", "seat_numbers")

    # Locks audit timestamps to view-only so they cannot be manually forged or edited.
    readonly_fields = ("created_at", "updated_at")

    # Embeds the related SagaEvent table directly inside each transaction's detail page.
    inlines = [SagaEventInline]

    # Organizes fields on the detail page into visually distinct, labeled sections with helpful descriptions.
    fieldsets = (
        ("Saga Transaction Overview", {
            "fields": ("transaction_id", "status", "amount", "user_id", "user_email", "created_at", "updated_at")
        }),
        ("Seat Booking Details", {
            "fields": ("seat_ids", "seat_numbers", "quantity"),
            "description": "Discrete physical seat identifier(s) in Inventory Service."
        }),
    )
