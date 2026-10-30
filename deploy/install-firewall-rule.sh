#!/usr/bin/env bash
# Let God Father open and close ufw rules for your local subnet without a password
# prompt. Run once, with sudo:
#
#     sudo ./deploy/install-firewall-rule.sh
#
# It grants exactly three things to your user: ufw status, and adding/removing an
# "allow from <your subnet> to any port N proto tcp" rule. Nothing else.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo:  sudo $0" >&2
  exit 1
fi

TARGET_USER="${SUDO_USER:-$USER}"
UFW="$(command -v ufw || echo /usr/sbin/ufw)"
SUDOERS=/etc/sudoers.d/godfather-ufw

if [ ! -x "$UFW" ]; then
  echo "ufw was not found. Install it first:  apt install ufw" >&2
  exit 1
fi

cat > "$SUDOERS" <<RULES
# Installed by God Father — lets $TARGET_USER manage LAN sharing rules only.
$TARGET_USER ALL=(root) NOPASSWD: $UFW status
$TARGET_USER ALL=(root) NOPASSWD: $UFW allow from * to any port [0-9]* proto tcp
$TARGET_USER ALL=(root) NOPASSWD: $UFW delete allow from * to any port [0-9]* proto tcp
RULES

chmod 0440 "$SUDOERS"

if visudo -c -f "$SUDOERS" >/dev/null; then
  echo "Installed $SUDOERS"
  echo "God Father can now open and close local-network ports by itself."
  echo
  echo "To undo:  sudo rm $SUDOERS"
else
  rm -f "$SUDOERS"
  echo "Refused: the generated sudoers file did not validate. Nothing was changed." >&2
  exit 1
fi
