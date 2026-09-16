from django.core.management.base import BaseCommand
from inventory.views import reclaim_expired_holds


class Command(BaseCommand):
    help = "Sweeps expired and abandoned seat holds and marks them as AVAILABLE in PostgreSQL."

    def handle(self, *args, **options):
        reclaimed, released = reclaim_expired_holds()

        if not reclaimed:
            self.stdout.write("No expired holds found.")
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Swept {reclaimed} expired seat hold(s), released {released} reservation(s)."
            )
        )
