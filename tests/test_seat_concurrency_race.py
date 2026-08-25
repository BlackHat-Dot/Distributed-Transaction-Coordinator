"""
Concurrent Seat Hold Race & Beacon Release Demonstration Test
=============================================================
Demonstrates:
1. Two concurrent clients attempting to hold the same seat simultaneously.
2. PostgreSQL select_for_update + Redis speed layer guarantees exactly one winner (HTTP 200) and one loser (HTTP 409 Conflict).
3. Best-effort beacon release on tab close (POST /seats/release/) immediately unlocks the seat to AVAILABLE.
"""

import time
import uuid
import requests
from concurrent.futures import ThreadPoolExecutor

INVENTORY_URL = "http://127.0.0.1:8000"
COORDINATOR_URL = "http://127.0.0.1:8002"


def run_concurrency_race_test(target_seat_id=5):
    print("=" * 70)
    print(f"STARTING CONCURRENT SEAT HOLD RACE DEMONSTRATION (Target Seat: {target_seat_id})")
    print("=" * 70)

    # 1. Reset venue catalog
    reset_res = requests.post(f"{INVENTORY_URL}/seats/reset/")
    assert reset_res.status_code == 200, f"Reset failed: {reset_res.text}"
    print("[Setup] All venue seats reset to AVAILABLE.")

    # 2. Prepare 2 competing client requests for the exact same seat
    client1_tx = str(uuid.uuid4())
    client2_tx = str(uuid.uuid4())

    def attempt_hold(client_label, tx_id):
        start = time.perf_counter()
        res = requests.post(
            f"{INVENTORY_URL}/seats/reserve/",
            json={"seat_id": target_seat_id, "transaction_id": tx_id},
            timeout=5,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        return client_label, res.status_code, res.json(), elapsed_ms, tx_id

    # 3. Fire both requests concurrently in parallel threads
    print(f"[Race] Dispatching 2 concurrent hold attempts for Seat #{target_seat_id}...")
    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(attempt_hold, "Client-1 (Alice)", client1_tx)
        f2 = executor.submit(attempt_hold, "Client-2 (Bob)", client2_tx)
        results = [f1.result(), f2.result()]

    for label, code, data, elapsed, tx_id in results:
        print(f"  -> {label}: HTTP {code} ({elapsed:.2f}ms) | Response: {data}")

    # Analyze outcome
    status_codes = [r[1] for r in results]
    assert 200 in status_codes, "Expected at least one winner (HTTP 200)"
    assert 409 in status_codes, "Expected exactly one conflict loser (HTTP 409)"

    winner = [r for r in results if r[1] == 200][0]
    loser = [r for r in results if r[1] == 409][0]

    print(f"\n[Race Result] Winner: {winner[0]} secured hold (HTTP 200).")
    print(f"[Race Result] Loser:  {loser[0]} rejected with HTTP 409 Conflict ('{loser[2].get('error')}').")

    # 4. Verify Seat Status in Inventory
    seats_res = requests.get(f"{INVENTORY_URL}/seats/")
    seat_5 = [s for s in seats_res.json() if s["id"] == target_seat_id][0]
    print(f"\n[Verification] Authoritative Database State for Seat #{target_seat_id}:")
    print(f"  -> Status:                 {seat_5['status']}")
    print(f"  -> Held Until:             {seat_5['held_until']}")
    print(f"  -> Held By Transaction ID: {seat_5['held_by_transaction_id']}")
    assert seat_5["status"] == "HELD"
    assert seat_5["held_by_transaction_id"] == winner[4]

    # 5. Simulate Tab Close / Unload Beacon Release
    print(f"\n[Beacon Test] Simulating {winner[0]} closing tab / firing sendBeacon release...")
    beacon_res = requests.post(
        f"{INVENTORY_URL}/seats/release/",
        json={"transaction_id": winner[4]},
        timeout=5,
    )
    print(f"  -> Beacon Release Response: HTTP {beacon_res.status_code} | {beacon_res.json()}")
    assert beacon_res.status_code == 200

    # 6. Verify Seat returned to AVAILABLE
    seats_after = requests.get(f"{INVENTORY_URL}/seats/")
    seat_5_after = [s for s in seats_after.json() if s["id"] == target_seat_id][0]
    print(f"\n[Verification Post-Beacon] Authoritative Database State for Seat #{target_seat_id}:")
    print(f"  -> Status:                 {seat_5_after['status']}")
    print(f"  -> Held By Transaction ID: {seat_5_after['held_by_transaction_id']}")
    assert seat_5_after["status"] == "AVAILABLE"
    assert seat_5_after["held_by_transaction_id"] is None
    print("\n[SUCCESS] Race condition & Beacon Release invariants verified 100%!")


if __name__ == "__main__":
    run_concurrency_race_test()
