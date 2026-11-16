#!/usr/bin/env bash
# Removes the udev rule and menu entry. Saved settings in ~/.config/x3ctl are kept.
set -euo pipefail
sudo rm -f /etc/udev/rules.d/70-attackshark-x3.rules
sudo udevadm control --reload-rules
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/io.github.x3ctl.desktop"
echo "Removed. Your saved settings are still in ~/.config/x3ctl."
