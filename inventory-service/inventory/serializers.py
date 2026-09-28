from rest_framework import serializers
from .models import Event, Seat, Reservation


from django.utils import timezone

class PublicSeatSerializer(serializers.ModelSerializer):
    """
    Public seat representation. Does NOT leak sensitive fields
    (held_by_transaction_id, user_id, user_email).
    Includes computed is_mine boolean based on the caller's JWT identity.
    """
    is_mine = serializers.SerializerMethodField()

    class Meta:
        model = Seat
        fields = [
            "id",
            "event",
            "row",
            "seat_number",
            "price",
            "status",
            "held_until",
            "is_mine",
        ]

    def get_is_mine(self, obj):
        request = self.context.get("request")
        if not request:
            return False

        auth_user = getattr(request, "_auth_user", None)
        if auth_user is None:
            from .views import _get_authenticated_user
            auth_user = _get_authenticated_user(request)
            request._auth_user = auth_user

        user_id = auth_user.get("user_id") if auth_user else None
        if not user_id:
            return False

        if obj.status == Seat.Status.HELD and obj.user_id and str(obj.user_id) == str(user_id):
            if obj.held_until and obj.held_until > timezone.now():
                return True
        return False

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        # Lazy Read-Time Shedding: Treat expired holds as AVAILABLE on read
        if rep.get("status") == "HELD" and instance.held_until and instance.held_until < timezone.now():
            rep["status"] = "AVAILABLE"
            rep["held_until"] = None
            rep["is_mine"] = False
        return rep


class InternalSeatSerializer(serializers.ModelSerializer):
    """
    Internal seat representation. Includes all audit/tracking fields
    (held_by_transaction_id, user_id, user_email) for Coordinator/Admin service consumers.
    """
    class Meta:
        model = Seat
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

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        # Lazy Read-Time Shedding: Treat expired holds as AVAILABLE on read
        if rep.get("status") == "HELD" and instance.held_until and instance.held_until < timezone.now():
            rep["status"] = "AVAILABLE"
            rep["held_until"] = None
            rep["held_by_transaction_id"] = None
            rep["user_id"] = None
            rep["user_email"] = None
        return rep


# Backward compatibility alias
SeatSerializer = InternalSeatSerializer


class EventSerializer(serializers.ModelSerializer):
    seats = PublicSeatSerializer(many=True, read_only=True)
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