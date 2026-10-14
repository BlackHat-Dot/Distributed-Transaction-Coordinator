import functools
import http.server
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright

GATE_SCANNER_DIR = Path(__file__).resolve().parent.parent / "gate-scanner"
GATE_URL = os.getenv("GATE_URL", "http://127.0.0.1:3001/index.html")
CORRECT_GATE_KEY = "gate-secret-key-change-in-production"


def get_fresh_booked_ticket(user_email=None):
    """
    Drives a real booking flow via API & UI to obtain a real booked ticket.
    """
    if user_email is None:
        import uuid
        user_email = f"fan_{uuid.uuid4().hex[:8]}@example.com"
    requests.post(f"{INVENTORY_URL}/seats/reset/", timeout=5)
    otp_resp = requests.post(f"{COORDINATOR_URL}/auth/send-otp/", json={"email": user_email}, timeout=5)
    otp_match = re.search(r"\b(\d{6})\b", otp_resp.json().get("message", ""))
    assert otp_match, f"OTP not found in {otp_resp.text}"
    otp_code = otp_match.group(1)

    verify_resp = requests.post(
        f"{COORDINATOR_URL}/auth/verify-otp/",
        json={"email": user_email, "otp": otp_code},
        timeout=5,
    )
    token = verify_resp.json()["token"]
    user_id = verify_resp.json()["user_id"]

    # Book via coordinator API
    inv_seats = requests.get(f"{INVENTORY_URL}/seats/", timeout=5).json()
    avail = [s for s in inv_seats if s.get("status") == "AVAILABLE"]
    assert avail, "No available seats found to book"
    chosen_seat = avail[0]
    seat_id = chosen_seat["id"]

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-User-Id": str(user_id),
        "X-User-Email": user_email,
    }
    tx_resp = requests.post(
        f"{COORDINATOR_URL}/transactions/?sync=true",
        headers=headers,
        json={"seat_ids": [seat_id]},
        timeout=10,
    )
    assert tx_resp.status_code in (200, 201), f"Booking failed: {tx_resp.status_code} {tx_resp.text}"
    tx_data = tx_resp.json()
    tx_id = tx_data["transaction_id"]

    # Fetch tickets
    ticket_resp = requests.get(f"{COORDINATOR_URL}/tickets/{tx_id}/", headers=headers, timeout=5)
    assert ticket_resp.status_code == 200, f"Get tickets failed: {ticket_resp.text}"
    tickets = ticket_resp.json()["tickets"]
    assert len(tickets) > 0, "No tickets returned"
    ticket = tickets[0]
    return {
        "transaction_id": tx_id,
        "ticket_code": ticket["ticket_code"],
        "seat_label": ticket["seat_label"],
        "event_name": ticket_resp.json()["event"]["name"],
        "token": token,
    }


class GateScannerPlaywrightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._httpd = None
        # Check if server is running, otherwise boot one serving gate-scanner
        if not cls._is_server_up(GATE_URL):
            port = 3001
            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(GATE_SCANNER_DIR))
            cls._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
            cls._server_thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
            cls._server_thread.start()
            time.sleep(0.5)

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        if cls._httpd:
            cls._httpd.shutdown()
            cls._httpd.server_close()

    @staticmethod
    def _is_server_up(url):
        try:
            r = requests.head(url, timeout=1)
            return r.status_code == 200
        except Exception:
            return False

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def _unlock_gate(self, page, key=CORRECT_GATE_KEY):
        page.goto(GATE_URL, wait_until="networkidle")
        page.fill("#gate-key-input", key)
        page.click("#unlock-gate-btn")
        page.wait_for_selector("#scanner-view:not(.hidden)", timeout=5000)

    def test_a_valid_ticket_admit_and_already_used_rescan(self):
        """Valid ticket scan -> ADMIT; re-scan (after debounce) -> ALREADY USED with original time."""
        ticket_info = get_fresh_booked_ticket()
        code = ticket_info["ticket_code"]
        verify_url = f"{COORDINATOR_URL}/verify/{code}/"

        self._unlock_gate(self.page)

        # 1. Scan valid ticket URL
        self.page.evaluate("url => window.__gateHandleScan(url)", verify_url)
        self.page.wait_for_selector("#result-overlay.active.result-admit", timeout=5000)
        status_text = self.page.text_content("#result-status")
        self.assertIn("ADMIT", status_text)
        self.assertIn(ticket_info["seat_label"], self.page.text_content("#result-seat"))
        self.assertIn(ticket_info["event_name"], self.page.text_content("#result-event"))

        # Admitted session counter incremented to 1
        counter_text = self.page.text_content("#session-counter")
        self.assertIn("1", counter_text)

        # Dismiss overlay and wait > 3s for debounce
        self.page.click("#result-overlay")
        self.page.wait_for_selector("#result-overlay", state="hidden", timeout=5000)
        time.sleep(3.2)

        # 2. Re-scan same ticket URL
        self.page.evaluate("url => window.__gateHandleScan(url)", verify_url)
        self.page.wait_for_selector("#result-overlay.active.result-already-used", timeout=5000)
        used_text = self.page.text_content("#result-status")
        self.assertIn("ALREADY USED", used_text)
        self.assertIn(ticket_info["seat_label"], self.page.text_content("#result-seat"))
        time_text = self.page.text_content("#result-used-time")
        self.assertTrue(len(time_text.strip()) > 0)

    def test_b_untrusted_origin_and_non_url_zero_requests(self):
        """Foreign host or non-URL -> NOT VALID with ZERO requests to /gate/redeem/."""
        self._unlock_gate(self.page)
        redeem_requests = []
        self.page.on("request", lambda req: redeem_requests.append(req.url) if "/gate/redeem/" in req.url else None)

        # 1. Malicious/untrusted origin
        evil_url = "https://evil-phishing-host.com/verify/someticketcode123456/"
        self.page.evaluate("url => window.__gateHandleScan(url)", evil_url)
        self.page.wait_for_selector("#result-overlay.active.result-not-valid", timeout=5000)
        self.assertIn("NOT VALID", self.page.text_content("#result-status"))
        self.assertEqual(len(redeem_requests), 0, "Network call was made for untrusted origin!")

        # Dismiss and wait
        self.page.click("#result-overlay")
        self.page.wait_for_selector("#result-overlay", state="hidden", timeout=5000)

        # 2. Non-URL text
        self.page.evaluate("text => window.__gateHandleScan(text)", "THIS_IS_NOT_A_URL")
        self.page.wait_for_selector("#result-overlay.active.result-not-valid", timeout=5000)
        self.assertIn("NOT VALID", self.page.text_content("#result-status"))
        self.assertEqual(len(redeem_requests), 0, "Network call was made for non-URL text!")

        # Dismiss and wait
        self.page.click("#result-overlay")
        self.page.wait_for_selector("#result-overlay", state="hidden", timeout=5000)

        # 3. Unknown-but-well-formed code (request DOES get made to server, returns 404 NOT_FOUND)
        unknown_url = f"{COORDINATOR_URL}/verify/unknowncode1234567890_/"
        self.page.evaluate("url => window.__gateHandleScan(url)", unknown_url)
        self.page.wait_for_selector("#result-overlay.active.result-not-valid", timeout=5000)
        self.assertIn("NOT VALID", self.page.text_content("#result-status"))
        self.assertEqual(len(redeem_requests), 1, "Expected 1 server request for well-formed unknown code")

    def test_b2_accepted_verify_origins_config(self):
        """QR from a listed origin that differs from the API origin is accepted."""
        self.page.goto(GATE_URL, wait_until="networkidle")
        # Configure acceptedVerifyOrigins with external origin
        self.page.evaluate("""() => {
            window.__GATE_CONFIG__ = {
                acceptedVerifyOrigins: ["https://trusted-venue-tickets.org"]
            };
        }""")
        self.page.fill("#gate-key-input", CORRECT_GATE_KEY)
        self.page.click("#unlock-gate-btn")
        self.page.wait_for_selector("#scanner-view:not(.hidden)", timeout=5000)

        redeem_payloads = []
        self.page.route("**/gate/redeem/**", lambda route: (
            redeem_payloads.append(route.request.post_data_json),
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"status":"REDEEMED","seat_label":"VIP-1","event_name":"Listed Origin Event","used_at":"2026-10-01T12:00:00Z"}'
            )
        ))

        # QR with listed external origin
        listed_qr = "https://trusted-venue-tickets.org/verify/code_from_listed_origin_123/"
        self.page.evaluate("url => window.__gateHandleScan(url)", listed_qr)
        self.page.wait_for_selector("#result-overlay.active.result-admit", timeout=5000)
        self.assertIn("ADMIT", self.page.text_content("#result-status"))
        self.assertEqual(len(redeem_payloads), 1)
        self.assertEqual(redeem_payloads[0]["ticket_code"], "code_from_listed_origin_123")

    def test_b3_subpath_verify_url_like_reverse_proxy(self):
        """Scanned QR URL with subpath (e.g. /coordinator/verify/<code>/) is correctly parsed and redeemed."""
        self._unlock_gate(self.page)
        redeem_payloads = []
        self.page.route("**/gate/redeem/**", lambda route: (
            redeem_payloads.append(route.request.post_data_json),
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"status":"REDEEMED","seat_label":"C5","event_name":"Subpath Tour","used_at":"2026-10-01T12:00:00Z"}'
            )
        ))

        subpath_qr = f"{COORDINATOR_URL}/coordinator/verify/code_with_subpath_12345/"
        self.page.evaluate("url => window.__gateHandleScan(url)", subpath_qr)
        self.page.wait_for_selector("#result-overlay.active.result-admit", timeout=5000)
        self.assertIn("ADMIT", self.page.text_content("#result-status"))
        self.assertEqual(len(redeem_payloads), 1)
        self.assertEqual(redeem_payloads[0]["ticket_code"], "code_with_subpath_12345")

    def test_c_wrong_key_rejected_and_never_leaked(self):
        """Wrong key on unlock -> rejected immediately on login view; in-session revocation -> KEY REJECTED & locked."""
        bad_key = "super-secret-wrong-key-99999"
        captured_logs = []
        captured_requests = []
        self.page.on("console", lambda msg: captured_logs.append(msg.text))
        self.page.on("request", lambda req: captured_requests.append(req.url))

        # 1. Immediate rejection at login
        self.page.goto(GATE_URL, wait_until="networkidle")
        self.page.fill("#gate-key-input", bad_key)
        self.page.click("#unlock-gate-btn")

        # Must display error and NOT unlock scanner view
        self.page.wait_for_selector("#key-entry-error:not(.hidden)", timeout=5000)
        self.assertIn("Invalid Gate API Key", self.page.text_content("#key-entry-error"))
        self.assertTrue(self.page.is_hidden("#scanner-view"))

        # Verify bad key was never leaked to URLs, storage, cookies, logs
        for req_url in captured_requests:
            self.assertNotIn(bad_key, req_url, f"Key leaked in request URL: {req_url}")

        local_storage_keys = self.page.evaluate("() => Object.keys(localStorage).map(k => localStorage.getItem(k)).join(' ')")
        self.assertNotIn(bad_key, local_storage_keys, "Key leaked into localStorage!")

        session_storage_keys = self.page.evaluate("() => Object.keys(sessionStorage).map(k => sessionStorage.getItem(k)).join(' ')")
        self.assertNotIn(bad_key, session_storage_keys, "Key leaked into sessionStorage!")

        cookies = self.page.evaluate("() => document.cookie")
        self.assertNotIn(bad_key, cookies, "Key leaked into cookies!")

        for log in captured_logs:
            self.assertNotIn(bad_key, log, f"Key leaked into console log: {log}")

        # 2. In-session key revocation handling: unlock with valid key, then server returns 401 on scan
        self.page.fill("#gate-key-input", CORRECT_GATE_KEY)
        self.page.click("#unlock-gate-btn")
        self.page.wait_for_selector("#scanner-view:not(.hidden)", timeout=5000)

        # Mock server revocation (401 on redeem)
        self.page.route("**/gate/redeem/**", lambda route: route.fulfill(
            status=401,
            content_type="application/json",
            body='{"error":"Invalid or missing gate API key.","code":"UNAUTHORIZED"}'
        ))

        self.page.evaluate("url => window.__gateHandleScan(url)", f"{COORDINATOR_URL}/verify/somecode123456789012/")
        self.page.wait_for_selector("#result-overlay.active.result-key-rejected", timeout=5000)
        self.assertIn("KEY REJECTED", self.page.text_content("#result-status"))

        # Dismiss overlay and ensure kick-back to key-entry with input cleared
        self.page.click("#result-overlay")
        self.page.wait_for_selector("#key-entry-view:not(.hidden)", timeout=5000)
        self.assertEqual(self.page.input_value("#gate-key-input"), "", "Key input was not cleared after KEY REJECTED!")

    def test_d_rate_limiting_slow_down(self):
        """11 bad-key attempts -> SLOW DOWN is shown."""
        self._unlock_gate(self.page)
        self.page.route("**/gate/redeem/**", lambda route: route.fulfill(
            status=429,
            content_type="application/json",
            body='{"error":"Rate limit exceeded on gate redemption.","code":"RATE_LIMITED"}'
        ))
        self.page.evaluate("url => window.__gateHandleScan(url)", f"{COORDINATOR_URL}/verify/rate_limit_code_12345/")
        self.page.wait_for_selector("#result-overlay.active.result-slow-down", timeout=5000)
        self.assertIn("SLOW DOWN", self.page.text_content("#result-status"))

    def test_e_network_failure_shows_network_error_never_admit(self):
        """Network failure / route abort -> NETWORK ERROR, never ADMIT."""
        self._unlock_gate(self.page)
        self.page.route("**/gate/redeem/**", lambda route: route.abort())

        self.page.evaluate("url => window.__gateHandleScan(url)", f"{COORDINATOR_URL}/verify/network_fail_code_123/")
        self.page.wait_for_selector("#result-overlay.active.result-network-error", timeout=5000)
        self.assertIn("NETWORK ERROR", self.page.text_content("#result-status"))
        self.assertNotIn("ADMIT", self.page.text_content("#result-status"))

    def test_f_real_decode_photo_file_upload(self):
        """Generate a real QR PNG, feed through file input with setInputFiles, assert ADMIT."""
        ticket_info = get_fresh_booked_ticket()
        code = ticket_info["ticket_code"]
        verify_url = f"{COORDINATOR_URL}/verify/{code}/"

        # Generate QR code PNG image file on disk
        import qrcode
        qr = qrcode.QRCode(box_size=10, border=2)
        qr.add_data(verify_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        temp_png = os.path.join(tempfile.gettempdir(), f"qr_test_{code}.png")
        img.save(temp_png)

        try:
            self._unlock_gate(self.page)
            # Use set_input_files on the photo capture input
            self.page.set_input_files("#qr-file-input", temp_png)

            # Wait for html5-qrcode to scan file and trigger ADMIT
            self.page.wait_for_selector("#result-overlay.active.result-admit", timeout=10000)
            self.assertIn("ADMIT", self.page.text_content("#result-status"))
            self.assertIn(ticket_info["seat_label"], self.page.text_content("#result-seat"))
        finally:
            if os.path.exists(temp_png):
                os.remove(temp_png)

    def test_g_concurrency_and_debounce(self):
        """Fire same scan twice rapidly -> exactly one request reaches the server."""
        self._unlock_gate(self.page)
        redeem_requests = []
        self.page.on("request", lambda req: redeem_requests.append(req.url) if "/gate/redeem/" in req.url else None)

        valid_url = f"{COORDINATOR_URL}/verify/rapid_scan_code_123456/"
        self.page.evaluate("""url => {
            Promise.all([
                window.__gateHandleScan(url),
                window.__gateHandleScan(url)
            ]);
        }""", valid_url)

        self.page.wait_for_selector("#result-overlay.active", timeout=5000)
        self.assertEqual(len(redeem_requests), 1, f"Expected 1 request, got {len(redeem_requests)}")

    def test_response_mappings(self):
        """Test mapped status codes: 500 -> error screen, 200 with wrong body -> error screen, 409 TRANSACTION_NOT_COMPLETED -> NOT VALID."""
        self._unlock_gate(self.page)

        # 1. Mock 500
        self.page.route("**/gate/redeem/**", lambda route: route.fulfill(
            status=500,
            content_type="text/plain",
            body="Internal Server Error"
        ))
        self.page.evaluate("url => window.__gateHandleScan(url)", f"{COORDINATOR_URL}/verify/code_mock_500_123456/")
        self.page.wait_for_selector("#result-overlay.active.result-network-error", timeout=5000)
        self.assertIn("NETWORK ERROR", self.page.text_content("#result-status"))
        self.assertNotIn("ADMIT", self.page.text_content("#result-status"))
        self.page.click("#result-overlay")
        time.sleep(0.5)

        # 2. Mock 200 with wrong body (status != "REDEEMED")
        self.page.unroute("**/gate/redeem/**")
        self.page.route("**/gate/redeem/**", lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"status":"UNEXPECTED_STATUS"}'
        ))
        self.page.evaluate("url => window.__gateHandleScan(url)", f"{COORDINATOR_URL}/verify/code_mock_wrong_200_12/")
        self.page.wait_for_selector("#result-overlay.active.result-network-error", timeout=5000)
        self.assertIn("NETWORK ERROR", self.page.text_content("#result-status"))
        self.assertNotIn("ADMIT", self.page.text_content("#result-status"))
        self.page.click("#result-overlay")
        time.sleep(0.5)

        # 3. Mock 409 TRANSACTION_NOT_COMPLETED
        self.page.unroute("**/gate/redeem/**")
        self.page.route("**/gate/redeem/**", lambda route: route.fulfill(
            status=409,
            content_type="application/json",
            body='{"error":"Cannot redeem ticket: transaction status is COMPENSATED, expected COMPLETED.","code":"TRANSACTION_NOT_COMPLETED"}'
        ))
        self.page.evaluate("url => window.__gateHandleScan(url)", f"{COORDINATOR_URL}/verify/code_mock_comp_123456/")
        self.page.wait_for_selector("#result-overlay.active.result-not-valid", timeout=5000)
        self.assertIn("NOT VALID", self.page.text_content("#result-status"))
        self.page.click("#result-overlay")
        time.sleep(0.5)

        # 4. Mock 400 BAD_REQUEST
        self.page.unroute("**/gate/redeem/**")
        self.page.route("**/gate/redeem/**", lambda route: route.fulfill(
            status=400,
            content_type="application/json",
            body='{"error":"ticket_code is required.","code":"BAD_REQUEST"}'
        ))
        self.page.evaluate("url => window.__gateHandleScan(url)", f"{COORDINATOR_URL}/verify/code_mock_bad_req_1234/")
        self.page.wait_for_selector("#result-overlay.active.result-not-valid", timeout=5000)
        self.assertIn("NOT VALID", self.page.text_content("#result-status"))


if __name__ == "__main__":
    unittest.main()
