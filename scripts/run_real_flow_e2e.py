import io
import json
import re
import sys
import requests
import cv2
import numpy as np
import pypdf

COORDINATOR_URL = "http://127.0.0.1:8002"
INVENTORY_URL = "http://127.0.0.1:8000"
PAYMENT_URL = "http://127.0.0.1:8001"
GATE_KEY = "gate-secret-key-change-in-production"
USER_EMAIL = "rockstar_fan@example.com"

def log(step, msg):
    print(f"[{step}] {msg}")

def main():
    print("=" * 70)
    print("STARTING REAL E2E FLOW AGAINST RUNNING SERVICES")
    print(f"Coordinator: {COORDINATOR_URL}")
    print(f"Inventory:   {INVENTORY_URL}")
    print(f"Payment:     {PAYMENT_URL}")
    print("=" * 70)

    # 1. Health checks
    for name, url in [("Inventory", INVENTORY_URL), ("Payment", PAYMENT_URL), ("Coordinator", COORDINATOR_URL)]:
        r = requests.get(f"{url}/health/", timeout=5)
        assert r.status_code == 200, f"{name} health check failed: {r.status_code}"
        log("HEALTH", f"{name} is UP: {r.json()}")

    # 2. OTP Login
    log("AUTH", f"Requesting OTP for {USER_EMAIL}...")
    otp_req = requests.post(f"{COORDINATOR_URL}/auth/send-otp/", json={"email": USER_EMAIL}, timeout=5)
    assert otp_req.status_code == 200, f"send-otp failed: {otp_req.text}"
    otp_data = otp_req.json()
    log("AUTH", f"send-otp response: {otp_data['message']}")
    
    # Extract OTP code from message (in dev mode: "[LOCAL DEV] Verification code: 123456...")
    match = re.search(r"\b(\d{6})\b", otp_data.get("message", ""))
    assert match, f"Could not find 6-digit OTP code in response: {otp_data}"
    otp_code = match.group(1)
    log("AUTH", f"Extracted OTP code: {otp_code}")

    # Verify OTP
    verify_req = requests.post(
        f"{COORDINATOR_URL}/auth/verify-otp/",
        json={"email": USER_EMAIL, "otp": otp_code},
        timeout=5,
    )
    assert verify_req.status_code == 200, f"verify-otp failed: {verify_req.text}"
    auth_data = verify_req.json()
    jwt_token = auth_data["token"]
    user_id = auth_data["user_id"]
    log("AUTH", f"Logged in successfully. User ID: {user_id}. JWT length: {len(jwt_token)}")

    auth_headers = {"Authorization": f"Bearer {jwt_token}"}

    # 3. Find available seat from Inventory
    log("SEATS", "Fetching available seats from Inventory catalog...")
    seats_resp = requests.get(f"{INVENTORY_URL}/seats/", timeout=5)
    assert seats_resp.status_code == 200, f"Failed to list seats: {seats_resp.text}"
    seats = seats_resp.json()
    avail_seats = [s for s in seats if s["status"] == "AVAILABLE"]
    assert avail_seats, "No available seats found in catalog"
    target_seat = avail_seats[0]
    seat_id = target_seat["id"]
    seat_label = f"{target_seat['row']}{target_seat['seat_number']}"
    log("SEATS", f"Selected seat {seat_label} (ID: {seat_id}, Price: ${target_seat['price']})")

    # 4. Hold seat via Coordinator
    log("BOOKING", f"Placing hold on seat {seat_id} via POST /transactions/ ...")
    hold_resp = requests.post(
        f"{COORDINATOR_URL}/transactions/?sync=true",
        headers=auth_headers,
        json={"action": "hold", "seat_ids": [seat_id]},
        timeout=10,
    )
    assert hold_resp.status_code == 200, f"Hold failed: {hold_resp.text}"
    hold_data = hold_resp.json()
    tx_id = hold_data["transaction_id"]
    log("BOOKING", f"Seat held. Transaction ID: {tx_id}, Status: {hold_data.get('status')}")

    # 5. Pay for seat via Coordinator (runs saga pipeline: charge + confirm)
    log("BOOKING", f"Triggering payment for transaction {tx_id} via POST /transactions/ ...")
    pay_resp = requests.post(
        f"{COORDINATOR_URL}/transactions/?sync=true",
        headers=auth_headers,
        json={"action": "pay", "transaction_id": tx_id},
        timeout=15,
    )
    assert pay_resp.status_code == 200, f"Payment failed: {pay_resp.text}"
    pay_data = pay_resp.json()
    assert pay_data["status"] == "COMPLETED", f"Expected COMPLETED, got {pay_data['status']}"
    log("BOOKING", f"Saga completed! Status: {pay_data['status']}, Amount: ${pay_data['amount']}")

    # 6. Lazy ticket issuance via GET /tickets/<tx_id>/
    log("TICKET", f"Fetching tickets via GET /tickets/{tx_id}/ ...")
    tix_resp = requests.get(
        f"{COORDINATOR_URL}/tickets/{tx_id}/",
        headers=auth_headers,
        timeout=10,
    )
    assert tix_resp.status_code == 200, f"Failed to get tickets: {tix_resp.text}"
    tix_data = tix_resp.json()
    tickets_list = tix_data["tickets"]
    assert len(tickets_list) == 1, f"Expected 1 ticket, got {len(tickets_list)}"
    ticket = tickets_list[0]
    ticket_code = ticket["ticket_code"]
    assert "qr_code_base64" in ticket
    log("TICKET", f"Ticket issued! Code: {ticket_code}, Seat: {ticket['seat_label']}, Event: {tix_data['event']['name']}")
    assert len(ticket_code) == 22, f"Expected 22-char token_urlsafe(16), got {len(ticket_code)}"

    # 7. Download PDF e-ticket via GET /tickets/<tx_id>/pdf/
    log("PDF", f"Downloading PDF via GET /tickets/{tx_id}/pdf/ ...")
    pdf_resp = requests.get(
        f"{COORDINATOR_URL}/tickets/{tx_id}/pdf/",
        headers=auth_headers,
        timeout=10,
    )
    assert pdf_resp.status_code == 200, f"Failed to download PDF: {pdf_resp.text}"
    assert pdf_resp.headers.get("Content-Type") == "application/pdf"
    content_disp = pdf_resp.headers.get("Content-Disposition", "")
    log("PDF", f"Content-Disposition: {content_disp}")
    assert f"DTC-{seat_label}" in content_disp, f"Expected seat label in filename, got {content_disp}"

    pdf_bytes = pdf_resp.content
    assert pdf_bytes.startswith(b"%PDF"), "PDF does not start with %PDF header"
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 1
    page_text = reader.pages[0].extract_text()
    log("PDF", f"Extracted PDF text:\n{page_text.strip()}")
    assert "DISTRIBUTED TRANSACTION COORDINATOR" in page_text
    assert "Scan at gate" in page_text
    assert "Coldplay" in page_text
    assert f"${target_seat['price']}" in page_text or "$" in page_text

    # 8. Extract QR Code image directly from downloaded PDF bytes and decode using OpenCV
    log("QR", "Extracting embedded QR code image from downloaded PDF bytes using pypdf...")
    assert len(reader.pages[0].images) > 0, "No images found embedded in the PDF page"
    pdf_image = reader.pages[0].images[0]
    log("QR", f"Found embedded image '{pdf_image.name}' in PDF page 1 ({len(pdf_image.data)} bytes)")

    # Decode embedded image bytes using OpenCV
    cv_img = cv2.imdecode(np.frombuffer(pdf_image.data, np.uint8), cv2.IMREAD_COLOR)
    assert cv_img is not None, "Failed to load PDF embedded image into OpenCV"

    detector = cv2.QRCodeDetector()
    decoded_url, _points, _ = detector.detectAndDecode(cv_img)
    log("QR", f"Decoded QR URL from PDF image bytes: {decoded_url}")
    assert f"/verify/{ticket_code}/" in decoded_url, f"Expected /verify/{ticket_code}/ in {decoded_url}"


    # 9. Public Verification (VALID)
    log("VERIFY", f"Calling public verification GET /verify/{ticket_code}/ ...")
    v1_resp = requests.get(
        decoded_url,
        headers={"Accept": "application/json"},
        timeout=5,
    )
    assert v1_resp.status_code == 200, f"Verify failed: {v1_resp.text}"
    v1_data = v1_resp.json()
    log("VERIFY", f"Status: {v1_data['status']}, Event: {v1_data['event_name']}, Seat: {v1_data['seat_label']}")
    assert v1_data["status"] == "VALID"
    assert v1_data["used_at"] is None
    # Ensure zero PII leakage
    for pii_field in ["email", "user_id", "transaction_id", "transaction_uuid"]:
        assert pii_field not in v1_data, f"PII leak detected: {pii_field}"

    # 10. First Gate Redemption (Success -> 200)
    log("GATE", "Staff scanning ticket at gate via POST /gate/redeem/ ...")
    g1_resp = requests.post(
        f"{COORDINATOR_URL}/gate/redeem/",
        headers={"X-Gate-Api-Key": GATE_KEY},
        json={"ticket_code": ticket_code},
        timeout=5,
    )
    assert g1_resp.status_code == 200, f"First redeem failed: {g1_resp.text}"
    g1_data = g1_resp.json()
    log("GATE", f"Redeem 1 Response: {g1_data['status']} - {g1_data['message']}")
    assert g1_data["status"] == "REDEEMED"
    assert g1_data["used_at"] is not None

    # 11. Public Verification after Redemption (ALREADY USED)
    log("VERIFY", "Verifying ticket status after first redemption...")
    v2_resp = requests.get(
        decoded_url,
        headers={"Accept": "application/json"},
        timeout=5,
    )
    assert v2_resp.status_code == 200
    v2_data = v2_resp.json()
    log("VERIFY", f"Status after scan: {v2_data['status']}, Used At: {v2_data['used_at']}")
    assert v2_data["status"] == "ALREADY USED"
    assert v2_data["used_at"] is not None

    # 12. Second Gate Redemption (Fraud attempt -> 409 Conflict)
    log("GATE", "Attempting second scan (duplicate admission) at gate...")
    g2_resp = requests.post(
        f"{COORDINATOR_URL}/gate/redeem/",
        headers={"X-Gate-Api-Key": GATE_KEY},
        json={"ticket_code": ticket_code},
        timeout=5,
    )
    log("GATE", f"Redeem 2 HTTP Status: {g2_resp.status_code}")
    assert g2_resp.status_code == 409, f"Expected 409 Conflict, got {g2_resp.status_code}: {g2_resp.text}"
    g2_data = g2_resp.json()
    log("GATE", f"Redeem 2 Response: {g2_data['code']} - {g2_data['error']}")
    assert g2_data["code"] == "ALREADY_USED"
    assert g2_data["used_at"] == g1_data["used_at"]

    print("=" * 70)
    print("ALL REAL E2E FLOW STEPS PASSED SUCCESSFULLY WITHOUT ORM BYPASS!")
    print("=" * 70)

if __name__ == "__main__":
    main()
