from decimal import Decimal
from django.db import models
from django.utils import timezone


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
    price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("500.00"))
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.AVAILABLE,
        db_index=True,
    )
    held_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when the ephemeral 10-minute hold expires.",
    )
    held_by_transaction_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Transaction ID currently holding or booking the seat.",
    )
    user_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        help_text="User ID holding or booking the seat.",
    )
    user_email = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
        help_text="User email holding or booking the seat.",
    )

    class Meta:
        unique_together = [("event", "row", "seat_number")]
        ordering = ["event", "row", "seat_number"]
        indexes = [
            models.Index(fields=["held_by_transaction_id", "status"], name="idx_seat_tx_status"),
            models.Index(fields=["user_id", "status", "held_until"], name="idx_seat_user_status_until"),
        ]

    @property
    def is_available(self):
        return self.status == self.Status.AVAILABLE

    @property
    def is_held(self):
        return self.status == self.Status.HELD

    @property
    def is_booked(self):
        return self.status == self.Status.BOOKED

    def is_expired(self, now=None):
        if not self.held_until:
            return False
        if now is None:
            now = timezone.now()
        return self.held_until < now

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
    transaction_id = models.UUIDField(db_index=True)
    user_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        help_text="User ID holding the reservation.",
    )
    user_email = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
        help_text="User email holding the reservation.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RESERVED,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["seat", "transaction_id"], name="unique_seat_transaction"),
            models.UniqueConstraint(
                fields=["seat"],
                condition=models.Q(status__in=["INVENTORY_RESERVED", "BOOKED"]),
                name="unique_active_seat_reservation",
            ),
        ]
        indexes = [
            models.Index(fields=["transaction_id", "status"], name="idx_res_tid_status"),
        ]

    def __str__(self):
        return f"Reservation({self.transaction_id}, seat={self.seat_id}, {self.status})"