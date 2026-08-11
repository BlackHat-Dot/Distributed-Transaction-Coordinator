from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Payment


@api_view(["POST"])
def charge_payment(request):
    transaction_id = request.data.get("transaction_id")
    amount = request.data.get("amount")

    # intentional

    if amount == 9999:
        
        return Response({
            "message": "intentional error"},
            status=400
        )

    if not transaction_id:
        Response(
            {"error": "transaction_id"},
            status=400
        )

    if not amount or amount <= 0:
        return Response(
            {"error": "amount must be greater than zero"},
            status=400
        )

    existing_payment = Payment.objects.filter(
        transaction_id=transaction_id
    ).first()

    if existing_payment:
        return Response({
            "message": "Payment already exists",
            "transaction_id": transaction_id,
            "amount": str(existing_payment.amount),
            "status": existing_payment.status
        })
    
    payment = Payment.objects.create(
        transaction_id=transaction_id,
        amount=amount,
        status="CHARGED"
    )

    return Response({
        "message": "Payment charged",
        "transaction_id": transaction_id,
        "amount": str(payment.amount),
        "status": payment.status
    })