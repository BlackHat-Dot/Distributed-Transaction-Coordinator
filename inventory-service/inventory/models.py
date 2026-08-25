from django.db import models


class Event(models.Model):
    name = models.CharField(max_length=200)
    venue = models.CharField(max_length=200, blank=True, default="")
    starts_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Seat(models.Model):

    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE"
        HELD = "HELD"
        BOOKED = "BOOKED"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="seats")
    row = models.CharField(max_length=10)
    seat_number = models.CharField(max_length=10)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=500.00)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.AVAILABLE,
    )
    held_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when the ephemeral 10-minute hold expires.",
    )
    held_by_transaction_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="Transaction ID currently holding or booking the seat.",
    )

    class Meta:
        unique_together = [("event", "row", "seat_number")]
        ordering = ["event", "row", "seat_number"]

    def __str__(self):
        return f"{self.event.name} - Row {self.row} Seat {self.seat_number} ({self.status})"


class Reservation(models.Model):

    class Status(models.TextChoices):
        RESERVED = "INVENTORY_RESERVED"
        BOOKED = "BOOKED"
        RELEASED = "INVENTORY_RELEASED"
        NOT_RESERVED = "NOT_RESERVED"

    seat = models.ForeignKey(
        Seat,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="reservations",
    )
    transaction_id = models.UUIDField(unique=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RESERVED,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Reservation({self.transaction_id}, seat={self.seat_id}, {self.status})"