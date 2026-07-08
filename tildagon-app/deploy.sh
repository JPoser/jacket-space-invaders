#!/usr/bin/env bash
# Deploy JacVaders to a real Tildagon over USB.
#
# Stages the app into build/ (keeping __pycache__ and other junk off the
# badge), then uploads to :apps/JacVaders with uv-managed mpremote and
# reboots the badge. Same pattern as jac-man's deploy.sh.
#
# Usage:
#   ./deploy.sh                          # auto-detect the badge's port
#   ./deploy.sh /dev/tty.usbmodem1101    # explicit port
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="JacVaders"
SRC="$HERE/$APP"
BUILD="$HERE/build/$APP"
PORT="${1:-}"

mp() {
    if [[ -n "$PORT" ]]; then
        uv run --with mpremote mpremote connect "$PORT" "$@"
    else
        uv run --with mpremote mpremote "$@"
    fi
}

echo "== staging $APP into $BUILD"
rm -rf "$BUILD"
mkdir -p "$BUILD"
cp "$SRC"/*.py "$SRC"/metadata.json "$BUILD/"

echo "== uploading to :apps/$APP (port: ${PORT:-auto-detect})"
# Clear the stale copy first so renamed/removed modules don't linger.
mp fs rm -r ":apps/$APP" 2>/dev/null || true
mp fs mkdir :apps 2>/dev/null || true
mp fs cp -r "$BUILD" ":apps/"
echo "== rebooting badge"
mp reset
echo "done. The app appears in the badge menu as '$APP'."
