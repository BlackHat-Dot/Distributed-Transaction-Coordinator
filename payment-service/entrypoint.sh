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

echo "[Payment] Starting service command: $@"
exec "$@"
