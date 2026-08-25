from django.core.management.base import BaseCommand
from inventory.views import sweep_expired_holds
from rest_framework.test import APIRequestFactory


class Command(BaseCommand):
    help = "Sweeps expired seat holds and purges their Redis fast-path lock keys."

    def handle(self, *args, **options):
        factory = APIRequestFactory()
        request = factory.post("/seats/sweep-expired/")
        response = sweep_expired_holds(request)
        reclaimed = response.data.get("reclaimed_count", 0)
        self.stdout.write(self.style.SUCCESS(f"Swept {reclaimed} expired seat hold(s)."))
