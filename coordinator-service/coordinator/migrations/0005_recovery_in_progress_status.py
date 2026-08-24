# Generated manually for safe-recovery-claiming feature.
# Registers RECOVERY_IN_PROGRESS (and CHARGED, which was absent from 0004)
# in Django's choices metadata.  No DDL column change is required because
# the status column is a plain VARCHAR(40) in Postgres; the choices list only
# affects form validation and the Django admin display.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('coordinator', '0004_alter_transaction_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='transaction',
            name='status',
            field=models.CharField(
                choices=[
                    ('STARTED', 'Started'),
                    ('INVENTORY_RESERVED', 'Inventory Reserved'),
                    ('INVENTORY_RESERVE_FAILED', 'Inventory Reserve Failed'),
                    ('INVENTORY_TIMEOUT', 'Inventory Timeout'),
                    ('INVENTORY_CONNECTION_ERROR', 'Inventory Connection Error'),
                    ('PAYMENT_PROCESSING', 'Payment Processing'),
                    ('PAYMENT_FAILED', 'Payment Failed'),
                    ('PAYMENT_TIMEOUT', 'Payment Timeout'),
                    ('PAYMENT_CONNECTION_ERROR', 'Payment Connection Error'),
                    ('COMPENSATING', 'Compensating'),
                    ('COMPENSATED', 'Compensated'),
                    ('COMPENSATION_FAILED', 'Compensation Failed'),
                    ('COMPENSATED_ON_RETRY', 'Compensated On Retry'),
                    ('RETRY_COMPENSATION_FAILED', 'Retry Compensation Failed'),
                    ('RECOVERY_IN_PROGRESS', 'Recovery In Progress'),
                    ('COMPLETED', 'Completed'),
                    ('TIMEOUT', 'Timeout'),
                    ('CONNECTION_FAILED', 'Connection Failed'),
                    ('CHARGED', 'Charged'),
                ],
                max_length=40,
            ),
        ),
    ]
