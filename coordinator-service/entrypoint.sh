#!/bin/bash
set -e

# Auto-detect and navigate to service directory if started from repo root
if [ ! -f "manage.py" ]; then
  if [ -d "coordinator-service" ]; then
    cd coordinator-service
  fi
fi

echo "[Coordinator] Checking PostgreSQL connectivity..."
python -c "
import os, socket, time
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
        print(f'[Coordinator] PostgreSQL at {host}:{port} is reachable.')
        break
    except Exception as e:
        time.sleep(0.8)
else:
    print(f'[Coordinator] Proceeding with startup.')
" || true

echo "[Coordinator] Applying database migrations..."
python manage.py migrate --noinput

if [ "${DEBUG,,}" = "true" ] || [ "$DEBUG" = "1" ]; then
  echo "[Coordinator] Ensuring default admin superuser exists (Development)..."
  python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='admin').exists():
    User.objects.create_superuser('admin', 'admin@example.com', 'admin')
    print('  -> Created default admin superuser (username: admin, pass: admin)')
" 2>/dev/null || true
fi

APP_DIR="$(pwd)"
export PORT="${PORT:-8002}"

echo "[Coordinator] Generating supervisord.conf for PORT=$PORT in $APP_DIR..."
cat > /tmp/supervisord.conf << SUPEOF
[supervisord]
nodaemon=true
logfile=/dev/stdout
logfile_maxbytes=0
loglevel=info
pidfile=/tmp/supervisord.pid

[program:gunicorn]
command=gunicorn coordinator_service.wsgi:application --bind 0.0.0.0:${PORT} --workers 2 --threads 2 --worker-class gthread --timeout 120
directory=${APP_DIR}
environment=PYTHONPATH="${APP_DIR}"
autostart=true
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0

[program:celery_worker]
command=celery -A coordinator_service worker -l info --concurrency=2 -Q coordinator_queue
directory=${APP_DIR}
environment=PYTHONPATH="${APP_DIR}"
autostart=true
autorestart=true
startsecs=5
startretries=3
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0

[program:celery_beat]
command=celery -A coordinator_service beat -l info
directory=${APP_DIR}
environment=PYTHONPATH="${APP_DIR}"
autostart=true
autorestart=true
startsecs=5
startretries=3
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
SUPEOF

if [ $# -eq 0 ]; then
  echo "[Coordinator] Starting supervisord (gunicorn + celery worker + celery beat)..."
  exec supervisord -c /tmp/supervisord.conf
else
  echo "[Coordinator] Starting custom command: $@"
  exec "$@"
fi
