from rest_framework import serializers
from .models import Event, Seat, Reservation


from django.utils import timezone

class SeatSerializer(serializers.ModelSerializer):
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
        # Backwards compatibility aliases
        rep["held_by_user_id"] = rep.get("user_id")
        rep["held_by_user_email"] = rep.get("user_email")
        rep["held_by_actor_id"] = rep.get("user_id")
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

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        rep["held_by_user_id"] = rep.get("user_id")
        rep["held_by_user_email"] = rep.get("user_email")
        rep["held_by_actor_id"] = rep.get("user_id")
        return rep