from django.core.management.base import BaseCommand
from django.utils import timezone
from inventory.models import Seat, Reservation


class Command(BaseCommand):
    help = "Sweeps expired seat holds and marks them as AVAILABLE in PostgreSQL."

    def handle(self, *args, **options):
        now = timezone.now()

        expired_ids = list(
            Seat.objects.filter(
                status=Seat.Status.HELD,
                held_until__lt=now,
            ).values_list("id", flat=True)
        )

        if not expired_ids:
            self.stdout.write("No expired holds found.")
            return

        released = Reservation.objects.filter(
            seat_id__in=expired_ids,
            status=Reservation.Status.RESERVED,
        ).update(status=Reservation.Status.RELEASED)

        reclaimed = Seat.objects.filter(id__in=expired_ids).update(
            status=Seat.Status.AVAILABLE,
            held_until=None,
            held_by_transaction_id=None,
            user_id=None,
            user_email=None,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Swept {reclaimed} expired seat hold(s), released {released} reservation(s)."
            )
        )
