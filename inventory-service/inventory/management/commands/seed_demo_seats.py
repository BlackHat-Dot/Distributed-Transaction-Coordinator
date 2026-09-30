from datetime import datetime, timedelta
from decimal import Decimal
import zoneinfo
from django.core.management.base import BaseCommand
from django.utils import timezone
from inventory.models import Event, Seat


class Command(BaseCommand):
    help = "Seeds demo event and seats for testing and development."

    def handle(self, *args, **options):
        event = Event.objects.first()
        if not event:
            event = Event.objects.create(
                name='Coldplay: Music of the Spheres World Tour',
                venue='Grand Arena Stadium',
                starts_at=datetime(2026, 11, 15, 19, 30, tzinfo=zoneinfo.ZoneInfo("Asia/Kolkata")),
            )

            self.stdout.write(f"Created event: {event.name}")

        seats_to_create = []
        for r, price in [('A', Decimal("750.00")), ('B', Decimal("500.00")), ('C', Decimal("350.00"))]:
            for num in range(1, 11):
                if not Seat.objects.filter(event=event, row=r, seat_number=str(num)).exists():
                    seats_to_create.append(
                        Seat(event=event, row=r, seat_number=str(num), price=price, status=Seat.Status.AVAILABLE)
                    )

        if seats_to_create:
            Seat.objects.bulk_create(seats_to_create)
            self.stdout.write(self.style.SUCCESS(f"Created {len(seats_to_create)} demo seats."))
        else:
            self.stdout.write("Demo seats already exist.")
