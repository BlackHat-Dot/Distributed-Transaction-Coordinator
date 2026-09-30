import os
import re
import shutil
import sys
import time
import requests
from playwright.sync_api import sync_playwright

COORDINATOR_URL = "http://127.0.0.1:8002"
INVENTORY_URL = "http://127.0.0.1:8000"
FRONTEND_URL = "http://127.0.0.1:3000/booking.html"
USER_EMAIL = "playwright_tester@example.com"
ARTIFACT_DIR = r"C:\Users\MADHAVAN\.gemini\antigravity\brain\2bd96e13-5f93-4a69-b686-0483c925ee01"


def main():
    print("=" * 70)
    print("RUNNING FRONTEND PLAYWRIGHT REAL UI INTERACTION VERIFICATION")
    print("=" * 70)

    # 1. Reset seats to ensure clean catalog
    requests.post(f"{INVENTORY_URL}/seats/reset/", timeout=5)

    # 2. Get real OTP & JWT
    otp_resp = requests.post(f"{COORDINATOR_URL}/auth/send-otp/", json={"email": USER_EMAIL}, timeout=5)
    match = re.search(r"\b(\d{6})\b", otp_resp.json().get("message", ""))
    assert match, f"OTP not found in {otp_resp.text}"
    otp_code = match.group(1)

    verify_resp = requests.post(f"{COORDINATOR_URL}/auth/verify-otp/", json={"email": USER_EMAIL, "otp": otp_code}, timeout=5)
    token = verify_resp.json()["token"]
    user_id = verify_resp.json()["user_id"]
    print(f"[AUTH] Obtained real signed JWT token for {USER_EMAIL}")

    # 3. Launch Playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.on("console", lambda msg: print(f"[BROWSER CONSOLE] {msg.type}: {msg.text}"))
        page.on("pageerror", lambda exc: print(f"[BROWSER PAGE ERROR] {exc}"))

        print(f"[BROWSER] Navigating to {FRONTEND_URL} ...")
        page.goto(FRONTEND_URL, wait_until="networkidle")

        # Inject auth state into localStorage so browser fan identity is logged in
        page.evaluate(f"""() => {{
            localStorage.setItem("dtc_auth_token", "{token}");
            localStorage.setItem("dtc_user_email", "{USER_EMAIL}");
            localStorage.setItem("dtc_user_id", "{user_id}");
        }}""")
        page.reload(wait_until="networkidle")

        # 4. Drive REAL UI: wait for seat matrix to load
        print("[UI] Waiting for catalog seats to render in venue matrix...")
        page.wait_for_selector(".seat-tile[data-status='AVAILABLE']", timeout=10000)

        # Select first available seat tile
        seat_tile = page.locator(".seat-tile[data-status='AVAILABLE']").first
        seat_id = seat_tile.get_attribute("data-seat-id")
        print(f"[UI] Clicking available seat tile on interactive map (ID: {seat_id}) ...")
        seat_tile.click()
        page.wait_for_timeout(1000)

        # Click bottom pay bar to acquire 10-minute hold
        print("[UI] Clicking '#btn-bottom-pay' to acquire hold...")
        page.wait_for_selector("#btn-bottom-pay:not([disabled])", timeout=10000)
        page.click("#btn-bottom-pay")
        page.wait_for_timeout(1500)

        # Click Proceed to Payment on Checkout Summary
        print("[UI] Clicking '#btn-proceed-pay' to launch Saga orchestration...")
        page.wait_for_selector("#btn-proceed-pay:not([disabled])", timeout=10000)
        page.click("#btn-proceed-pay")

        # 5. Modal appears via real saga flow
        print("[UI] Waiting for #saga-modal to open...")
        page.wait_for_selector("#saga-modal:not(.hidden)", timeout=10000)

        # 6. Wait for distributed saga to complete and e-ticket card to appear
        print("[UI] Waiting for distributed saga to complete and e-ticket card to render...")
        page.wait_for_selector("#eticket-card:not(.hidden)", timeout=20000)
        page.wait_for_timeout(1500)

        # 7. Assert real event metadata from API (no hardcoded text)
        event_name = page.text_content("#ticket-event-name").strip()
        venue_time = page.text_content("#ticket-venue-name").strip()
        print(f"[VERIFY MODAL] Event Name: {repr(event_name)}")
        print(f"[VERIFY MODAL] Venue & Time: {repr(venue_time)}")
        assert "Coldplay: Music of the Spheres World Tour" in event_name, f"Expected real event name, got: {event_name}"
        assert "Grand Arena Stadium" in venue_time, f"Expected real venue name, got: {venue_time}"
        assert "15 Nov 2026" in venue_time or "IST" in venue_time, f"Expected real IST date/time, got: {venue_time}"
        print("  -> Verified: Modal shows real event name, venue, and IST date/time from API!")

        # 8. Assert Step 2 reads "Payment charged"
        step2_desc = page.text_content("#step-2-desc").strip()
        print(f"[VERIFY MODAL] Step 2 Description: {repr(step2_desc)}")
        assert "Payment charged" in step2_desc, f"Expected 'Payment charged', got: {step2_desc}"
        print("  -> Verified: Step 2 reads 'Payment charged'!")

        # 9. Assert real QR is shown (not .qr-mock)
        qr_container_class = page.get_attribute("#ticket-qr-container", "class") or ""
        print(f"[VERIFY QR] #ticket-qr-container class: {repr(qr_container_class)}")
        assert "qr-mock" not in qr_container_class, "Found forbidden .qr-mock class on QR container!"

        page.wait_for_selector("#ticket-qr-img:not(.hidden)", timeout=8000)
        qr_src = page.get_attribute("#ticket-qr-img", "src") or ""
        assert qr_src.startswith("data:image/png;base64,"), f"Expected real base64 QR, got: {qr_src[:50]}"
        print(f"[VERIFY QR] Real QR image rendered! Data URI length: {len(qr_src)}")

        # 10. Assert "Inspect Forensic Audit Log" styling has text-decoration: none
        forensic_link_style = page.evaluate("() => window.getComputedStyle(document.getElementById('btn-view-forensics')).textDecoration")
        print(f"[VERIFY STYLE] Inspect Forensic Audit Log text-decoration: {repr(forensic_link_style)}")
        assert "none" in forensic_link_style.lower(), f"Expected text-decoration: none, got {forensic_link_style}"

        # 11. Assert Download PDF button triggers file download
        print("[VERIFY DOWNLOAD] Clicking 'Download PDF' and asserting browser file download...")
        with page.expect_download() as download_info:
            page.click("#btn-download-pdf")
        download = download_info.value
        download_name = download.suggested_filename
        print(f"[VERIFY DOWNLOAD] Suggested filename: {download_name}")
        assert download_name.endswith(".pdf"), f"Expected .pdf extension, got: {download_name}"
        assert "DTC" in download_name
        download_path = f"visualizer/{download_name}"
        download.save_as(download_path)
        assert os.path.exists(download_path)
        print(f"  -> Verified: Downloaded {download_path} ({os.path.getsize(download_path)} bytes)")
        os.remove(download_path)

        # 12. Assert readable error states (404 and 401)
        print("[VERIFY ERROR 404] Testing readable error display on 404...")
        page.evaluate("() => window.loadAndRenderTicket('00000000-0000-0000-0000-000000000000')")
        page.wait_for_timeout(1000)
        error_el = page.locator("#eticket-error")
        assert not error_el.is_hidden(), "Expected #eticket-error to be visible on 404"
        err_404_text = error_el.text_content()
        print(f"[VERIFY ERROR 404] Rendered error: {repr(err_404_text)}")
        assert "404" in err_404_text or "not found" in err_404_text.lower()

        print("[VERIFY ERROR 401] Testing readable error display on 401...")
        tx_id_text = page.text_content("#ticket-tx-id").strip()
        page.evaluate(f"""() => {{
            localStorage.removeItem("dtc_auth_token");
            window.loadAndRenderTicket('{tx_id_text}');
        }}""")
        page.wait_for_timeout(1000)
        assert not error_el.is_hidden(), "Expected #eticket-error to be visible on 401"
        err_401_text = error_el.text_content()
        print(f"[VERIFY ERROR 401] Rendered error: {repr(err_401_text)}")
        assert "401" in err_401_text or "log in" in err_401_text.lower()

        # 13. Re-render valid ticket and capture verified screenshot
        page.evaluate(f"""() => {{
            localStorage.setItem("dtc_auth_token", "{token}");
            window.loadAndRenderTicket('{tx_id_text}');
        }}""")
        page.wait_for_selector("#ticket-qr-img:not(.hidden)", timeout=5000)
        page.wait_for_timeout(500)
        screenshot_path = "visualizer/e_ticket_modal_verified.png"
        page.locator(".modal-card").screenshot(path=screenshot_path)
        print(f"[SCREENSHOT] Saved verified modal screenshot to {screenshot_path}")

        # Copy to artifact dir
        if os.path.exists(ARTIFACT_DIR):
            artifact_target = os.path.join(ARTIFACT_DIR, "e_ticket_modal_verified.png")
            shutil.copyfile(screenshot_path, artifact_target)
            print(f"[ARTIFACT] Copied screenshot to {artifact_target}")

        browser.close()

    print("=" * 70)
    print("ALL REAL UI PLAYWRIGHT CHECKS PASSED WITH FLYING COLORS!")
    print("=" * 70)


if __name__ == "__main__":
    main()
