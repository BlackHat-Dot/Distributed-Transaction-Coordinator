from decimal import Decimal
from django.db import models
from django.utils import timezone


# Represents an entertainment event, concert, or theatrical performance hosted at a physical venue.
class Event(models.Model):
    name = models.CharField(max_length=200)
    venue = models.CharField(max_length=200, blank=True, default="")
    # Stores the event commencement timestamp in UTC to coordinate localized ticket display and schedule reminders.
    starts_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Seat(models.Model):

    # Defines the finite lifecycle states of a physical seat: AVAILABLE (free), HELD (in-flight saga hold), or BOOKED (confirmed).
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE"
        HELD = "HELD"
        BOOKED = "BOOKED"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="seats")
    row = models.CharField(max_length=10)
    seat_number = models.CharField(max_length=10)
    # Declares the admission price using fixed-point Decimal to prevent floating-point rounding errors during financial charge calculations.
    price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("500.00"))
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.AVAILABLE,
        db_index=True,
    )
    # Records the precise UTC instant when an in-flight hold expires, enabling background sweepers and queries to evict stale locks.
    held_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when the ephemeral 10-minute hold expires.",
    )
    # Binds the physical seat to the holding or booking saga transaction UUID to enforce mutual exclusion across distributed buyers.
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
        # Prevents duplicate physical seat records at the database engine level, guaranteeing coordinate integrity within each venue event.
        unique_together = [("event", "row", "seat_number")]
        ordering = ["event", "row", "seat_number"]
        indexes = [
            # Optimizes transactional lookups during saga confirmations and compensations by indexing transaction UUID and seat status together.
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

    # Compares the seat's held_until timestamp against current UTC time to evaluate whether an unconfirmed hold has elapsed.
    def is_expired(self, now=None):
        if not self.held_until:
            return False
        if now is None:
            now = timezone.now()
        return self.held_until < now

    # Formats readable seat coordinate descriptor (e.g. "Concert - Row A Seat 12 (HELD)") for admin and debug logging.
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
    released_reason = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Audit reason for release: e.g. ADMIN_RESET, EXPIRED_HOLD, USER_CANCELLED, COORDINATOR_COMPENSATED",
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