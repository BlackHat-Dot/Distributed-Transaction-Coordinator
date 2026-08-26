#!/bin/bash
set -e

# Auto-detect and navigate to service directory if started from repo root
if [ ! -f "manage.py" ]; then
  if [ -d "payment-service" ]; then
    cd payment-service
  fi
fi

echo "[Payment] Checking PostgreSQL connectivity..."
python -c "
import os, sys, socket, time
from urllib.parse import urlparse

db_url = os.getenv('DATABASE_URL') or os.getenv('POSTGRES_URL')
if db_url:
    u = urlparse(db_url)
    host = u.hostname or 'localhost'
    port = u.port or 5432
else:
    host = os.getenv('PGHOST', os.getenv('DB_HOST', 'postgres'))
    port = int(os.getenv('PGPORT', os.getenv('DB_PORT', '5432')))

for attempt in range(20):
    try:
        s = socket.socket()
        s.settimeout(2)
        s.connect((host, port))
        s.close()
        print(f'[Payment] PostgreSQL at {host}:{port} is reachable.')
        break
    except Exception as e:
        time.sleep(0.8)
else:
    print(f'[Payment] Proceeding with startup.')
" || true

echo "[Payment] Ensuring isolated database 'payment_db' exists on PostgreSQL..."
python -c "
import os, psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from urllib.parse import urlparse

db_url = os.getenv('DATABASE_URL') or os.getenv('POSTGRES_URL')
target_db = os.getenv('DB_NAME', 'payment_db')

if db_url and os.getenv('SHARE_SINGLE_DB') != 'true':
    u = urlparse(db_url)
    maintenance_db = u.path.lstrip('/') or 'postgres'
    try:
        conn = psycopg2.connect(
            dbname=maintenance_db,
            user=u.username,
            password=u.password,
            host=u.hostname,
            port=u.port or 5432
        )
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor()
        cur.execute('SELECT 1 FROM pg_database WHERE datname = %s', (target_db,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE {target_db}')
            print(f'  -> Created isolated database: {target_db}')
        else:
            print(f'  -> Database {target_db} is ready.')
        cur.close()
        conn.close()
    except Exception as e:
        print(f'  -> Note on DB check: {e}')
" || true

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
mkdir -p staticfiles
python manage.py collectstatic --noinput

PORT="${PORT:-8001}"
if [ $# -eq 0 ]; then
  echo "[Payment] Starting Gunicorn on 0.0.0.0:$PORT..."
  exec gunicorn payment_service.wsgi:application --bind "0.0.0.0:$PORT" --workers 4 --threads 4 --worker-class gthread --timeout 30
else
  echo "[Payment] Starting custom command: $@"
  exec "$@"
fi
