from django.db import migrations
from django.db.models import Q


def populate_unknown_legacy(apps, schema_editor):
    Reservation = apps.get_model("inventory", "Reservation")
    Reservation.objects.filter(
        status__in=["INVENTORY_RELEASED", "RELEASED"]
    ).filter(
        Q(released_reason="") | Q(released_reason__isnull=True)
    ).update(released_reason="UNKNOWN_LEGACY")


def reverse_func(apps, schema_editor):
    Reservation = apps.get_model("inventory", "Reservation")
    Reservation.objects.filter(released_reason="UNKNOWN_LEGACY").update(
        released_reason=""
    )


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0017_reservation_released_reason"),
    ]

    operations = [
        migrations.RunPython(populate_unknown_legacy, reverse_func),
    ]
