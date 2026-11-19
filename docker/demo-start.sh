#!/bin/sh
# Start command of the public demo (render.yaml). Runs on every boot: a free instance sleeps when idle and
# boots again on the next visit, and the database may be brand new.
#   1. migrations (no-op when up to date)
#   2. the API on the host's $PORT, trusting its proxy for the client IP (the live per-IP cap needs it;
#      FORWARDED_ALLOW_IPS is read by uvicorn, which `fastapi run` starts with --proxy-headers)
set -e
alembic upgrade head
export FORWARDED_ALLOW_IPS='*'
exec fastapi run app/main.py --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers
