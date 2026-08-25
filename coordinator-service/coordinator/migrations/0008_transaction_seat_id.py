from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('coordinator', '0007_saga_event_model'),
    ]

    operations = [
        migrations.AddField(
            model_name='transaction',
            name='seat_id',
            field=models.IntegerField(null=True),
        ),
    ]
