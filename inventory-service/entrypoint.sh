#!/bin/bash
set -e

DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"

echo "[Inventory] Waiting for PostgreSQL ($DB_HOST:$DB_PORT)..."
while ! python -c "import socket; s = socket.socket(); s.settimeout(1); s.connect(('$DB_HOST', int('$DB_PORT'))); s.close()" 2>/dev/null; do
  sleep 0.5
done
echo "[Inventory] PostgreSQL is reachable."

echo "[Inventory] Applying database migrations..."
python manage.py migrate --noinput

echo "[Inventory] Ensuring seed event/seat catalog exists..."
python -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'inventory_service.settings')
django.setup()
from inventory.models import Event, Seat
from django.utils import timezone
from datetime import timedelta
if Event.objects.count() == 0:
    event = Event.objects.create(name='World Premiere Concert', venue='Grand Hall', starts_at=timezone.now() + timedelta(days=14))
    for r in ['A', 'B', 'C']:
        for num in range(1, 11):
            Seat.objects.create(event=event, row=r, seat_number=str(num), price=500.00)
    print('  -> Seeded 1 Event and 30 Seats.')
" 2>/dev/null || true

echo "[Inventory] Starting service command: $@"
exec "$@"
