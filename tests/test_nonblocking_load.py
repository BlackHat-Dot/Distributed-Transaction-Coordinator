"""
Non-Blocking Coordinator Concurrency & WSGI Thread Starvation Load Test
========================================================================
This test fires concurrent transaction submissions against the Coordinator while
measuring latency of concurrent health check and transaction acceptance requests.
It proves that downstream I/O does not block the Coordinator's WSGI request threads.
"""

import time
import uuid
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

COORDINATOR_URL = "http://127.0.0.1:8002"

# Configure thread-safe session with connection pooling
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50)
session.mount("http://", adapter)


def ping_health():
    start = time.perf_counter()
    res = session.get(f"{COORDINATOR_URL}/health/", timeout=5)
    elapsed_ms = (time.perf_counter() - start) * 1000
    return res.status_code, elapsed_ms


def submit_async_transaction(tx_index):
    tid = str(uuid.uuid4())
    seat_id = (tx_index % 30) + 1
    amount = 500
    start = time.perf_counter()
    res = session.post(
        f"{COORDINATOR_URL}/transactions/",
        json={"transaction_id": tid, "seat_id": seat_id, "amount": amount},
        timeout=10,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    return res.status_code, elapsed_ms, tid, res.json()


def run_load_test(concurrent_txns=20, concurrent_health_pings=20):
    print("=" * 70)
    print(f"STARTING NON-BLOCKING CONCURRENCY LOAD TEST ({concurrent_txns} TXNs + {concurrent_health_pings} Pings)")
    print("=" * 70)

    # 1. Warm-up ping
    status, warmup_ms = ping_health()
    print(f"[Baseline] Health check latency: {warmup_ms:.2f}ms (HTTP {status})")

    # 2. Fire concurrent transactions and interleaved health checks simultaneously
    tx_results = []
    health_results = []

    start_all = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrent_txns + concurrent_health_pings) as executor:
        # Submit transaction tasks
        tx_futures = [executor.submit(submit_async_transaction, i) for i in range(concurrent_txns)]
        # Interleave health pings during transaction burst
        health_futures = [executor.submit(ping_health) for _ in range(concurrent_health_pings)]

        for f in as_completed(tx_futures):
            tx_results.append(f.result())

        for f in as_completed(health_futures):
            health_results.append(f.result())

    total_burst_time = time.perf_counter() - start_all

    # 3. Analyze Transaction Submission Metrics
    tx_statuses = [r[0] for r in tx_results]
    tx_latencies = [r[1] for r in tx_results]
    accepted_202_count = tx_statuses.count(202)
    completed_200_count = tx_statuses.count(200)

    avg_tx_latency = sum(tx_latencies) / len(tx_latencies)
    max_tx_latency = max(tx_latencies)
    min_tx_latency = min(tx_latencies)

    # 4. Analyze Interleaved Health Check Metrics
    health_latencies = [r[1] for r in health_results]
    avg_health_latency = sum(health_latencies) / len(health_latencies)
    min_health_latency = min(health_latencies)
    max_health_latency = max(health_latencies)

    print("\n--- RESULTS & METRICS ---")
    print(f"Total Requests Dispatched:        {concurrent_txns + concurrent_health_pings}")
    print(f"Total Burst Execution Duration:   {total_burst_time:.3f}s")
    print(f"Transaction Accept Responses:     {accepted_202_count} accepted (HTTP 202), {completed_200_count} completed")
    print(f"Avg Transaction Accept Latency:   {avg_tx_latency:.2f}ms (Min: {min_tx_latency:.2f}ms, Max: {max_tx_latency:.2f}ms)")
    print(f"Avg Health Check Latency:         {avg_health_latency:.2f}ms (Min: {min_health_latency:.2f}ms, Max: {max_health_latency:.2f}ms)")
    print(f"Thread Starvation Detected:       {'NO (Non-Blocking Verified)' if avg_health_latency < 50 else 'YES'}")
    print("=" * 70)

    # 5. Wait briefly and poll one transaction to prove Celery completed it in background
    sample_tid = tx_results[0][2]
    time.sleep(1.0)
    poll_res = session.get(f"{COORDINATOR_URL}/transactions/{sample_tid}/")
    polled_data = poll_res.json()
    print(f"\n[Celery Verification] Polled sample transaction ({sample_tid}):")
    print(f"  -> Final Status: {polled_data.get('status')}")
    print(f"  -> Is Terminal:  {polled_data.get('is_terminal')}")

    # Inspect SagaEvent count
    events_res = session.get(f"{COORDINATOR_URL}/transactions/{sample_tid}/events/")
    events_data = events_res.json()
    print(f"  -> SagaEvents Recorded: {len(events_data.get('events', []))} events")

    return {
        "accepted_202": accepted_202_count,
        "avg_tx_latency_ms": avg_tx_latency,
        "avg_health_latency_ms": avg_health_latency,
        "sample_final_status": polled_data.get("status"),
    }


if __name__ == "__main__":
    run_load_test()
