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
event = Event.objects.first()
if not event:
    event = Event.objects.create(name='Coldplay: Music of the Spheres World Tour', venue='Grand Arena Stadium', starts_at=timezone.now() + timedelta(days=14))
if Seat.objects.count() == 0:
    for r, price in [('A', 750.00), ('B', 500.00), ('C', 350.00)]:
        for num in range(1, 11):
            Seat.objects.create(event=event, row=r, seat_number=str(num), price=price, status=Seat.Status.AVAILABLE)
    print('  -> Seeded 30 Seats across Rows A, B, and C.')
" 2>/dev/null || true

echo "[Inventory] Ensuring default admin superuser exists..."
python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='admin').exists():
    User.objects.create_superuser('admin', 'admin@example.com', 'admin')
    print('  -> Created default admin superuser (username: admin, pass: admin)')
" 2>/dev/null || true

echo "[Inventory] Collecting static files..."
python manage.py collectstatic --noinput

echo "[Inventory] Starting service command: $@"
exec "$@"
