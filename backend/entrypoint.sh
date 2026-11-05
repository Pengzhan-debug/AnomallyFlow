#!/bin/sh
set -eu

alembic upgrade head
if [ "${DEMO_MODE:-0}" = "1" ]; then
  python -m qualityflow.seed
fi
exec uvicorn qualityflow.api:app --host 0.0.0.0 --port 8000

