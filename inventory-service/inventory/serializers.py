from rest_framework import serializers
from .models import Event, Seat, Reservation


from django.utils import timezone

class SeatSerializer(serializers.ModelSerializer):
    # class Meta(data about data) tells DRF how to automatically build the serializer from database model
    class Meta:
        model = Seat    # tells DRF which database model to inspect

        # tells DRF which fields to include in the JSON payload
        # Inclusion: Only the 10 fields listed will be converted to JSOn when reading seats,
        # or accepted when writing/updating seats.
        fields = [
            "id",
            "event",
            "row",
            "seat_number",
            "price",
            "status",
            "held_until",
            "held_by_transaction_id",
            "user_id",
            "user_email",
        ]

    # assume timezone.now() is 2026-09-23 19:15:00 UTC
    def to_representation(self, instance):
        """
        instance = Seat(
            id=42,
            event_id=1,
            row="B",
            seat_number="12",
            price=Decimal("150.00"),
            status="HELD",
            held_until=datetime(2026, 9, 23, 19, 5, 0, tzinfo=timezone.utc),  # Expired 10 min ago!
            held_by_transaction_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            user_id="usr_9a8b",
            user_email="vijay@gmail.com",
        )
        """
        rep = super().to_representation(instance)

        """
        If instance is missing an attribute listed in Meta.fields, Python crashes right inside super().to_representation(instance) with:

        AttributeError: Got AttributeError when attempting to get a value for field 'xyz' on serializer 'SeatSerializer'...
        """
        # DRF's base ModelSerializer maps the model instance's database fields into primitive Python data types (strings, ints, floats, etc.)
        """
        rep =
        {
            "id": 42,
            "event": 1,
            "row": "B",
            "seat_number": "12",
            "price": "150.00",
            "status": "HELD",
            "held_until": "2026-09-23T19:05:00Z",
            "held_by_transaction_id": "550e8400-e29b-41d4-a716-446655440000",
            "user_id": "usr_9a8b",
            "user_email": "vijay@gmail.com"
        }
        """
        # Lazy Read-Time Shedding: Treat expired holds as AVAILABLE on read
        # held_until is extracted from instance because inside instance held_util is native Python datetime object
        # but inside rep["held_until"], DRF's DateTimeField is already converted it into a string.
        if rep.get("status") == "HELD" and instance.held_until and instance.held_until < timezone.now():
            rep["status"] = "AVAILABLE"
            rep["held_until"] = None
            rep["held_by_transaction_id"] = None
            rep["user_id"] = None
            rep["user_email"] = None
        return rep


class EventSerializer(serializers.ModelSerializer):
    seats = SeatSerializer(many=True, read_only=True)

    class Meta:
        model = Event
        fields = ["id", "name", "venue", "starts_at", "created_at", "seats"]


class ReservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Reservation
        fields = [
            "id",
            "seat",
            "transaction_id",
            "status",
            "user_id",
            "user_email",
            "created_at",
            "updated_at",
        ]