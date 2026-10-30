#!/usr/bin/env bash
# Install God Father as a systemd user service so it runs whenever the machine is on.
#
#   ./deploy/install-service.sh            # install on port 9100
#   PORT=9200 ./deploy/install-service.sh  # install on another port
#
# Safe to re-run: it rebuilds, rewrites the unit and restarts the service.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-9100}"
HOST="${HOST:-127.0.0.1}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/godfather.service"

say() { printf '\n\033[1;34m→ %s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- backend

say "Preparing the Python environment"
cd "$ROOT/backend"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

# --------------------------------------------------------------- frontend

say "Building the web interface"
cd "$ROOT/frontend"
if [ ! -d node_modules ]; then
  npm install --no-audit --no-fund
fi
npm run build

# ------------------------------------------------------------------- unit

say "Writing $UNIT"
mkdir -p "$UNIT_DIR"

# The install path may contain spaces, so every path in the unit is quoted.
cat > "$UNIT" <<UNITFILE
[Unit]
Description=God Father — application, log and port manager
Documentation=http://$HOST:$PORT/docs
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
WorkingDirectory=$ROOT/backend
ExecStart="$ROOT/backend/.venv/bin/uvicorn" app.main:app --host $HOST --port $PORT
Restart=always
RestartSec=3

# Signal only the main process, so its shutdown hook can stop managed apps
# cleanly; anything still alive after the timeout is killed with the cgroup.
KillMode=mixed
TimeoutStopSec=25

Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
UNITFILE

say "Enabling the service"
systemctl --user daemon-reload
systemctl --user enable godfather.service
systemctl --user restart godfather.service

# ---------------------------------------------------------------- linger

if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null || echo no)" != "yes" ]; then
  cat <<LINGER

  ────────────────────────────────────────────────────────────────────
  One step left, and it needs sudo.

  User services normally start only after you log in. To have God Father
  running from boot — before or without any login — enable lingering:

      sudo loginctl enable-linger $USER

  ────────────────────────────────────────────────────────────────────
LINGER
fi

sleep 2
say "Status"
systemctl --user --no-pager status godfather.service | head -12 || true

cat <<DONE

  God Father is at  http://$HOST:$PORT

  Useful commands:
    systemctl --user status godfather     # is it running?
    systemctl --user restart godfather    # restart it
    systemctl --user stop godfather       # stop it (also stops managed apps)
    journalctl --user -u godfather -f     # the service's own log

DONE
