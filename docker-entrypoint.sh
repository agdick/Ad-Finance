#!/bin/sh
set -e
# Apply database migrations, then start the web app (one worker: the sync scheduler runs in-process).
if [ "$#" -gt 0 ]; then
  exec "$@"
fi
alembic upgrade head
exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
