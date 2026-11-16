#!/usr/bin/env bash
# Installs the udev rule (needs sudo once) and a menu entry for the current user.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULE=70-attackshark-x3.rules

echo "Installing udev rule to /etc/udev/rules.d/$RULE (asks for your password)…"
sudo install -m 644 "$HERE/packaging/$RULE" "/etc/udev/rules.d/$RULE"
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw

APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
sed "s|@HERE@|$HERE|g" "$HERE/packaging/io.github.x3ctl.desktop" > "$APPS/io.github.x3ctl.desktop"
chmod +x "$HERE/x3ctl.sh"
update-desktop-database "$APPS" 2>/dev/null || true

echo
echo "Done. If the app still says it has no permission, unplug and replug the receiver."
echo "Start it from the app menu (\"Attack Shark X3\") or with: $HERE/x3ctl.sh"
