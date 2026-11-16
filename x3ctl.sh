#!/usr/bin/env bash
# Launches the Attack Shark X3 configurator from wherever this folder lives.
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$HERE" && exec python3 -m x3ctl "$@"
