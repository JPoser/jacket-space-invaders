#!/usr/bin/env python3
"""
Upload queued JacVaders high scores from the badge to jacket-server.

The badge saves every human game into /jacvaders_scores.json on its
flash (a local top-10 plus a `pending` queue of entries not yet on the
server). Plug the badge in over USB, run this, and the pending queue is
POSTed to the server and cleared. Radio-free by design: the badge never
touches WiFi, this tool is the whole sync path.

Usage:
  python3 tools/upload_scores.py                 # auto-detect badge port
  python3 tools/upload_scores.py --port /dev/cu.usbmodemXXX
  python3 tools/upload_scores.py --dry-run       # show, don't send
  python3 tools/upload_scores.py --keep          # send but don't clear

The API key comes from (in order): --key, the JACKET_API_KEY
environment variable, or tildagon-app/JacVaders/secrets.py.

Needs `mpremote` on PATH, or `uv` (used as `uv run --with mpremote`).
Exits non-zero if anything was left unsent.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

SCORE_FILE = "/jacvaders_scores.json"
DEFAULT_SERVER = "https://jacket.londonaero.space"
DEFAULT_GAME = "jacvaders"
# Anything identifiable that isn't Python-urllib's default: Cloudflare's
# browser integrity check 403s that one specifically.
USER_AGENT = "jacket-score-uploader/1.0"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_PATH = os.path.join(REPO_ROOT, "tildagon-app", "JacVaders", "secrets.py")


def mpremote_base():
    """The mpremote invocation to prefix, however it's installed."""
    if shutil.which("mpremote"):
        return ["mpremote"]
    if shutil.which("uv"):
        return ["uv", "run", "--with", "mpremote", "mpremote"]
    sys.exit("need mpremote (pip install mpremote) or uv on PATH")


def find_port(explicit):
    if explicit:
        return explicit
    candidates = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
    if not candidates:
        sys.exit("no badge found (looked for /dev/cu.usbmodem* and /dev/ttyACM*)")
    if len(candidates) > 1:
        sys.exit("several candidate ports: {} - pick one with --port".format(
            ", ".join(candidates)))
    return candidates[0]


def find_key(explicit):
    if explicit:
        return explicit
    env = os.environ.get("JACKET_API_KEY")
    if env:
        return env
    try:
        with open(SECRETS_PATH) as f:
            match = re.search(r'HIGHSCORE_API_KEY\s*=\s*"([^"]+)"', f.read())
        if match and match.group(1) != "YOUR_API_KEY":
            return match.group(1)
    except OSError:
        pass
    sys.exit("no API key: pass --key, set JACKET_API_KEY, or fill in {}".format(
        SECRETS_PATH))


def mpremote_exec(base, port, code):
    result = subprocess.run(
        base + ["connect", port, "exec", code],
        capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        sys.exit("mpremote failed: {}".format(
            (result.stderr or result.stdout).strip()[:300]))
    return result.stdout


def read_scores(base, port):
    """Fetch the score file; a missing file is just an empty table."""
    out = mpremote_exec(base, port, (
        "try:\n"
        "    print(open({!r}).read())\n"
        "except OSError:\n"
        "    print('{{}}')\n").format(SCORE_FILE))
    try:
        return json.loads(out.strip() or "{}")
    except ValueError:
        sys.exit("could not parse badge score file: {!r}".format(out[:200]))


def write_scores(base, port, data):
    mpremote_exec(base, port, "open({!r}, 'w').write({!r})".format(
        SCORE_FILE, json.dumps(data)))


def submit(server, key, game, entry):
    """POST one entry; returns the server's rank, or None on failure."""
    body = {"game": game, "name": entry.get("name", "AAA"),
            "score": entry.get("score", 0), "wave": entry.get("wave"),
            "difficulty": entry.get("difficulty")}
    request = urllib.request.Request(
        server.rstrip("/") + "/api/v1/scores",
        data=json.dumps(body).encode(),
        headers={"X-API-Key": key, "Content-Type": "application/json",
                 "User-Agent": USER_AGENT},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode())
        return payload.get("entry", {}).get("rank")
    except (urllib.error.URLError, OSError, ValueError) as e:
        print("  FAILED: {}".format(e))
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Upload queued badge high scores to jacket-server")
    parser.add_argument("--port", help="badge serial port (default: auto)")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--game", default=DEFAULT_GAME)
    parser.add_argument("--key", help="API key (or JACKET_API_KEY / secrets.py)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the queue, upload nothing")
    parser.add_argument("--keep", action="store_true",
                        help="upload but leave the badge queue intact")
    args = parser.parse_args()

    base = mpremote_base()
    port = find_port(args.port)
    print("badge: {}".format(port))

    data = read_scores(base, port)
    pending = list(data.get("pending", []))
    if not pending:
        print("nothing queued - the arcade is settled up")
        return

    print("{} score(s) queued:".format(len(pending)))
    for entry in pending:
        print("  {}  {:>7}  wave {}  {}".format(
            entry.get("name", "AAA"), entry.get("score", 0),
            entry.get("wave", "-"), entry.get("difficulty", "-")))
    if args.dry_run:
        return

    key = find_key(args.key)
    sent = []
    for entry in pending:
        rank = submit(args.server, key, args.game, entry)
        if rank is None:
            break  # stop; everything unsent stays queued
        sent.append(entry)
        print("  {} {} -> on the board at #{}".format(
            entry.get("name", "AAA"), entry.get("score", 0), rank))

    if sent and not args.keep:
        data["pending"] = pending[len(sent):]
        write_scores(base, port, data)
        print("badge queue updated ({} left)".format(len(data["pending"])))

    print("{}/{} uploaded - {}/scores".format(
        len(sent), len(pending), args.server.rstrip("/")))
    print("(power-cycle the badge to restart the badge OS)")
    if len(sent) < len(pending):
        sys.exit(1)


if __name__ == "__main__":
    main()
