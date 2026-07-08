#!/bin/sh
set -e
PORT="${PORT:-8000}"
PY="$(command -v python3 2>/dev/null || command -v python)"
if [ -z "$PY" ]; then
  echo "python3 not found in PATH" >&2
  exit 1
fi
exec "$PY" -m uvicorn app:app --host 0.0.0.0 --port "$PORT"
