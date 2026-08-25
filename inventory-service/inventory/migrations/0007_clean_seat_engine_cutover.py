# Migration 0007: Clean Schema Cutover from Generic Product Stock to Real-Time Seat Allocation Engine
#
# DESIGN & CUTOVER RATIONALE:
# This migration executes a clean schema replacement:
# 1. The legacy generic `Product(name, stock)` model is dropped entirely, eliminating the scalar decrement model.
# 2. Introduces `Event(name, venue, starts_at)` to represent bookable shows/events.
# 3. Introduces `Seat(event, row, seat_number, price, status, held_until, held_by_transaction_id)`
#    supporting two-phase seat allocation ([AVAILABLE, HELD, BOOKED]) and ephemeral hold timeouts.
# 4. Updates `Reservation` to directly reference `Seat` rather than scalar `product`/`quantity`.
#
# This clean cutover eliminates all obsolete stock counters and avoids carrying dead schema baggage.

import django.db.models.deletion
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0006_alter_reservation_status_fix_choices'),
    ]

    operations = [
        migrations.CreateModel(
            name='Event',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
                ('venue', models.CharField(blank=True, default='', max_length=200)),
                ('starts_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name='Seat',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('row', models.CharField(max_length=10)),
                ('seat_number', models.CharField(max_length=10)),
                ('price', models.DecimalField(decimal_places=2, default=500.0, max_digits=10)),
                ('status', models.CharField(choices=[('AVAILABLE', 'Available'), ('HELD', 'Held'), ('BOOKED', 'Booked')], default='AVAILABLE', max_length=20)),
                ('held_until', models.DateTimeField(blank=True, help_text='Timestamp when the ephemeral 10-minute hold expires.', null=True)),
                ('held_by_transaction_id', models.UUIDField(blank=True, help_text='Transaction ID currently holding or booking the seat.', null=True)),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='seats', to='inventory.event')),
            ],
            options={
                'ordering': ['event', 'row', 'seat_number'],
                'unique_together': {('event', 'row', 'seat_number')},
            },
        ),
        migrations.RemoveField(
            model_name='reservation',
            name='product',
        ),
        migrations.RemoveField(
            model_name='reservation',
            name='quantity',
        ),
        migrations.DeleteModel(
            name='Product',
        ),
        migrations.AddField(
            model_name='reservation',
            name='seat',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='reservations', to='inventory.seat'),
        ),
        migrations.AddField(
            model_name='reservation',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='reservation',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.AlterField(
            model_name='reservation',
            name='status',
            field=models.CharField(
                choices=[
                    ('INVENTORY_RESERVED', 'Reserved'),
                    ('BOOKED', 'Booked'),
                    ('INVENTORY_RELEASED', 'Released'),
                    ('NOT_RESERVED', 'Not Reserved'),
                ],
                default='INVENTORY_RESERVED',
                max_length=20,
            ),
        ),
    ]
