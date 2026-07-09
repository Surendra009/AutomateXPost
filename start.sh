#!/bin/sh
set -e
PORT="${PORT:-8000}"
if command -v python3 >/dev/null 2>&1; then
  exec python3 -m uvicorn app:app --host 0.0.0.0 --port "$PORT"
fi
exec python -m uvicorn app:app --host 0.0.0.0 --port "$PORT"
