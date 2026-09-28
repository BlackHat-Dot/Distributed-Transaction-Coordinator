from django.db import migrations
from django.db.models import Case, When, Value, IntegerField, Count
from inventory.models import Reservation


def dedupe_reservations(apps, schema_editor):
    ReservationModel = apps.get_model("inventory", "Reservation")
    db_alias = schema_editor.connection.alias

    # Explicit ranking using Reservation.Status constants:
    # BOOKED (3) > RESERVED (2) > RELEASED (1) > NOT_RESERVED (0)
    status_rank = Case(
        When(status=Reservation.Status.BOOKED, then=Value(3)),
        When(status=Reservation.Status.RESERVED, then=Value(2)),
        When(status=Reservation.Status.RELEASED, then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )

    # 1. Dedupe duplicate (seat, transaction_id) pairs
    dup_pairs = (
        ReservationModel.objects.using(db_alias)
        .values("seat_id", "transaction_id")
        .annotate(cnt=Count("id"))
        .filter(cnt__gt=1)
    )

    for pair in dup_pairs:
        seat_id = pair["seat_id"]
        tid = pair["transaction_id"]
        rows = list(
            ReservationModel.objects.using(db_alias)
            .filter(seat_id=seat_id, transaction_id=tid)
            .annotate(rank=status_rank)
            .order_by("-rank", "-updated_at", "-id")
        )
        # Keep top ranked row
        # For remaining duplicate rows: NEVER delete BOOKED rows
        for dup in rows[1:]:
            if dup.status == Reservation.Status.BOOKED:
                # Demote duplicate BOOKED to RELEASED to preserve audit record without deleting
                dup.status = Reservation.Status.RELEASED
                dup.save(update_fields=["status"])
            else:
                dup.delete()

    # 2. Resolve multiple active reservations per seat (seat has >1 row with RESERVED or BOOKED)
    active_seat_dups = (
        ReservationModel.objects.using(db_alias)
        .filter(status__in=[Reservation.Status.RESERVED, Reservation.Status.BOOKED])
        .values("seat_id")
        .annotate(cnt=Count("id"))
        .filter(cnt__gt=1)
    )

    for item in active_seat_dups:
        seat_id = item["seat_id"]
        active_rows = list(
            ReservationModel.objects.using(db_alias)
            .filter(seat_id=seat_id, status__in=[Reservation.Status.RESERVED, Reservation.Status.BOOKED])
            .annotate(rank=status_rank)
            .order_by("-rank", "-updated_at", "-id")
        )
        # Keep top active row (BOOKED ranked highest, then newest)
        # For other active rows on this seat: NEVER delete BOOKED rows; demote to RELEASED
        for other in active_rows[1:]:
            other.status = Reservation.Status.RELEASED
            other.save(update_fields=["status"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0014_alter_seat_held_by_transaction_id_alter_seat_status"),
    ]

    operations = [
        migrations.RunPython(dedupe_reservations, migrations.RunPython.noop),
    ]
