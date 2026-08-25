#!/bin/bash
set -e

DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"

echo "[Payment] Waiting for PostgreSQL ($DB_HOST:$DB_PORT)..."
while ! python -c "import socket; s = socket.socket(); s.settimeout(1); s.connect(('$DB_HOST', int('$DB_PORT'))); s.close()" 2>/dev/null; do
  sleep 0.5
done
echo "[Payment] PostgreSQL is reachable."

echo "[Payment] Applying database migrations..."
python manage.py migrate --noinput

echo "[Payment] Ensuring default admin superuser exists..."
python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='admin').exists():
    User.objects.create_superuser('admin', 'admin@example.com', 'admin')
    print('  -> Created default admin superuser (username: admin, pass: admin)')
" 2>/dev/null || true

echo "[Payment] Collecting static files..."
python manage.py collectstatic --noinput

echo "[Payment] Starting service command: $@"
exec "$@"
