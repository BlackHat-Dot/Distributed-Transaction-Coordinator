#!/bin/bash
set -e

DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"

echo "[Coordinator] Waiting for PostgreSQL ($DB_HOST:$DB_PORT)..."
while ! python -c "import socket; s = socket.socket(); s.settimeout(1); s.connect(('$DB_HOST', int('$DB_PORT'))); s.close()" 2>/dev/null; do
  sleep 0.5
done
echo "[Coordinator] PostgreSQL is reachable."

echo "[Coordinator] Applying database migrations..."
python manage.py migrate --noinput

echo "[Coordinator] Starting service command: $@"
exec "$@"
