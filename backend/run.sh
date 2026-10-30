#!/usr/bin/env bash
# Start the God Father backend.
#   ./run.sh            -> http://127.0.0.1:8000
#   ./run.sh 9100       -> http://127.0.0.1:9100
#   PORT=9100 ./run.sh  -> same
set -e
cd "$(dirname "$0")"

PORT="${1:-${PORT:-8000}}"
HOST="${HOST:-127.0.0.1}"

if [ ! -d ".venv" ]; then
  echo "→ creating virtualenv"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

echo "→ God Father backend on http://$HOST:$PORT  (docs at /docs)"
exec ./.venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
