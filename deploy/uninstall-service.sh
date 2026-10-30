#!/usr/bin/env bash
# Remove the God Father systemd user service. Leaves your data and logs alone.
set -euo pipefail

UNIT="$HOME/.config/systemd/user/godfather.service"

systemctl --user stop godfather.service 2>/dev/null || true
systemctl --user disable godfather.service 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload

echo "Service removed. Registered apps, logs and the database are untouched."
echo "Lingering, if you enabled it, stays on: sudo loginctl disable-linger $USER"
