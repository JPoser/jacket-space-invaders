#!/bin/bash
# Run the Tildagon badge simulator with JacVaders symlinked in.
#
# Assumes:
#   - https://github.com/emfcamp/badge-2024-software is cloned to
#     ../../badge-2024-software (sibling of jacket-space-invaders)
#   - uv is installed
#
# The simulator needs the local patches vendored in jacket-client
# (../../jacket-client/tildagon-app/sim-patches/) to drive an 84-LED
# hexpansion strip — they add the "Jacket strip" pygame window that shows
# the 6×14 grid. When jacket-client is checked out alongside, they're
# applied idempotently here, same as its own run_sim.sh does.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SIM_DIR="$(cd "$HERE/../../badge-2024-software/sim" && pwd)"
BADGE_DIR="$(cd "$SIM_DIR/.." && pwd)"
PATCHES="$HERE/../../jacket-client/tildagon-app/sim-patches"

if [ -d "$PATCHES" ]; then
    for p in "$PATCHES"/*.patch; do
        if git -C "$BADGE_DIR" apply --check --reverse "$p" >/dev/null 2>&1; then
            continue    # already applied
        elif git -C "$BADGE_DIR" apply --check "$p" >/dev/null 2>&1; then
            echo "Applying sim patch: $(basename "$p")"
            git -C "$BADGE_DIR" apply "$p"
        else
            echo "WARNING: $(basename "$p") no longer applies cleanly —" \
                 "the sim files have drifted; fix by hand." >&2
        fi
    done
else
    echo "WARNING: jacket-client not found at ../../jacket-client —" \
         "the sim will crash writing an 84-LED strip without its patches." >&2
fi

cd "$SIM_DIR"

if [ ! -d .venv ]; then
    echo "Creating venv (first run)..."
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -r requirements.txt
fi

ln -sfn "$HERE/JacVaders" apps/JacVaders

if [ ! -f config.py ]; then
    cp config.py.default config.py
fi

echo "Launching simulator. Open 'JacVaders' from the launcher."
exec .venv/bin/python run.py
