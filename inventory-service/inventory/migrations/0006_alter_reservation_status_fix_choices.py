# Fixes migration 0005 which had incorrect choices (used enum *names* instead
# of enum *values*) and was missing NOT_RESERVED entirely.
#
# The underlying PostgreSQL column (VARCHAR(20)) has never stored wrong data
# because choices validation is Python-only.  This migration corrects the
# recorded choices so that the migration history accurately reflects the model.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0005_alter_reservation_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='reservation',
            name='status',
            field=models.CharField(
                choices=[
                    ('INVENTORY_RESERVED', 'Reserved'),
                    ('INVENTORY_RELEASED', 'Released'),
                    ('NOT_RESERVED', 'Not Reserved'),
                ],
                default='INVENTORY_RESERVED',
                max_length=20,
            ),
        ),
    ]
