import os
import sys
import time
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

BASE_ENV = os.environ.copy()
BASE_ENV.update({
    "DEBUG": "True",
    "SECRET_KEY": "django-insecure-local-dev-secret-key-for-e2e-testing-only",
    "INTERNAL_SERVICE_KEY": "dtc-internal-coordinator-secret-key-2026",
    "GATE_API_KEY": "gate-secret-key-change-in-production",
    "DB_USER": "postgres",
    "DB_PASSWORD": "postgres",
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "5432",
    "CACHE_BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    "ENABLE_TEST_PAYMENT_FAILURE": "True",
    "ENABLE_FAULT_INJECTION": "True",
    "INVENTORY_SERVICE_URL": "http://127.0.0.1:8000",
    "PAYMENT_SERVICE_URL": "http://127.0.0.1:8001",
})

COORDINATOR_ENV = BASE_ENV.copy()
COORDINATOR_ENV.update({
    "INVENTORY_URL": "http://127.0.0.1:8000",
    "PAYMENT_URL": "http://127.0.0.1:8001",
    "INVENTORY_SERVICE_URL": "http://127.0.0.1:8000",
    "PAYMENT_SERVICE_URL": "http://127.0.0.1:8001",
    "PUBLIC_BASE_URL": "http://127.0.0.1:8002",
})

print("Starting Inventory Service on 8000...")
inv_proc = subprocess.Popen(
    [PYTHON, "inventory-service/manage.py", "runserver", "127.0.0.1:8000", "--noreload"],
    cwd=str(ROOT_DIR),
    env=BASE_ENV
)

print("Starting Payment Service on 8001...")
pay_proc = subprocess.Popen(
    [PYTHON, "payment-service/manage.py", "runserver", "127.0.0.1:8001", "--noreload"],
    cwd=str(ROOT_DIR),
    env=BASE_ENV
)

print("Starting Coordinator Service on 8002...")
coord_proc = subprocess.Popen(
    [PYTHON, "coordinator-service/manage.py", "runserver", "127.0.0.1:8002", "--noreload"],
    cwd=str(ROOT_DIR),
    env=COORDINATOR_ENV
)

print("Starting Visualizer (Client Booking) on 3000...")
vis_proc = subprocess.Popen(
    [PYTHON, "-m", "http.server", "3000", "--directory", "visualizer"],
    cwd=str(ROOT_DIR),
    env=BASE_ENV
)

print("Starting Gate Scanner (Staff Turnstile) on 3001...")
gate_proc = subprocess.Popen(
    [PYTHON, "-m", "http.server", "3001", "--directory", "gate-scanner"],
    cwd=str(ROOT_DIR),
    env=BASE_ENV
)

print("All services launched! Keeping alive...")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("Terminating services...")
    for p in (inv_proc, pay_proc, coord_proc, vis_proc, gate_proc):
        p.terminate()
