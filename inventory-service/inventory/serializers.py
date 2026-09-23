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
        
        # DRF's base ModelSerializer maps the model instance's database fields into primitive Python data types (strings, ints, floats, etc.)
        rep = super().to_representation(instance)

        """
        If instance is missing an attribute listed in Meta.fields, Python crashes right inside super().to_representation(instance) with:

        AttributeError: Got AttributeError when attempting to get a value for field 'xyz' on serializer 'SeatSerializer'...
        """
        
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
    # 1. How django knows about seats?

    # The Event models doesnt have a `seats` column, However when Django
    # loads your models at startup it reads Seat:

    # class Seat(models.Model):
    # event = models.ForeignKey(Event, on_delete=models.CASCADE,
    #  related_name="seats")

    # When Django sees related_name="seats", it dynamically attaches a reverse descriptor
    # attribute named seats onto Event class

    # Whenever python executes event.seats, Django intercepts it and returns a RelatedManager that runs:
    #   Seat.objects.filter(event=event)

    # if related_name="seats" is mentioned then django defaults name event.seat_set

    # ------------------------------------------------------------------------------------------------ #

    # 2. How DRF knows seats is the source

    # DRF’s metaclass inspects the class attributes:

    # It sees a field named seats.
    # It checks if you passed source="...".
    # Since you did not pass source, DRF binds the field name directly:
 
    # # DRF internal binding:
    # if field.source is None:
    #     field.source = "seats"  # Defaults to the `variable` name

    # ------------------------------------------------------------------------------------------------ #

    """
    # 1. Look up attribute matching variable name 'seats' on event
    manager = getattr(event, "seats")       # -> event.seats
    
    # 2. Get the related seat objects
    queryset = manager.all()                # -> Seat.objects.filter(event=event) # cache hit, no query executed
    
    # 3. Pass them to SeatSerializer because of many=True
    output = [SeatSerializer(seat).data for seat in queryset]
    """

    """ (If beginner read this to understand what is happening --> Vijay)
    1. Fetching (Event.objects.all().prefetch_related("seats")):
    Django fetches the Event records and, in a second batched query, fetches all related Seat records via the 
    foreign key relation, storing them together in Python memory (RAM).

    2. Need for Serialization:
    events is a Django QuerySet containing Python model instances (with complex types like datetime and Decimal),
    which cannot be sent over the network as JSON without serialization.

    3. Invoking the Serializer (EventSerializer(events, many=True)):
    We pass the queryset to DRF. Because many=True is specified, DRF prepares to serialize a list of event objects.

    4. Triggering Data Transformation (.data):
    Accessing .data executes the transformation. DRF checks Meta.model and Meta.fields to map standard database
    columns (name, venue, starts_at) to JSON-safe primitives.

    5. Handling Related Objects (seats):
    When DRF encounters the "seats" field, it recognizes the custom declaration (seats = SeatSerializer(...)).
    Instead of auto-generating a field, it passes the prefetched Seat objects from memory to SeatSerializer to format each seat into JSON.
    """
    class Meta:
        model = Event
        fields = ["id", "name", "venue", "starts_at", "created_at", "seats"]


# Not currently in use
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