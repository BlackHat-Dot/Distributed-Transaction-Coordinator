from rest_framework import serializers
from .models import Event, Seat, Reservation


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
        ]


class EventSerializer(serializers.ModelSerializer):
    seats = SeatSerializer(many=True, read_only=True)

    class Meta:
        model = Event
        fields = ["id", "name", "venue", "starts_at", "created_at", "seats"]


class ReservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Reservation
        fields = ["id", "seat", "transaction_id", "status", "created_at", "updated_at"]