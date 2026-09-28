from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0015_dedupe_reservations"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="seat",
            index=models.Index(fields=["held_by_transaction_id", "status"], name="idx_seat_tx_status"),
        ),
        migrations.AddIndex(
            model_name="seat",
            index=models.Index(fields=["user_id", "status", "held_until"], name="idx_seat_user_status_until"),
        ),
        migrations.AddIndex(
            model_name="reservation",
            index=models.Index(fields=["transaction_id", "status"], name="idx_res_tid_status"),
        ),
        migrations.AddConstraint(
            model_name="reservation",
            constraint=models.UniqueConstraint(fields=["seat", "transaction_id"], name="unique_seat_transaction"),
        ),
        migrations.AddConstraint(
            model_name="reservation",
            constraint=models.UniqueConstraint(
                condition=models.Q(status__in=["INVENTORY_RESERVED", "BOOKED"]),
                fields=["seat"],
                name="unique_active_seat_reservation",
            ),
        ),
    ]
