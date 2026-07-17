"""
High scores: local top-10 table, arcade initials entry, and submission
to jacket-server over camp WiFi.

Local-first by design: the table lives in a JSON file on badge flash and
works with no network at all. Submission to the server (POST
/api/v1/scores) is a bonus pass that runs at game over — the one moment
the jacket is just showing a marquee, so nobody misses the gamepad while
the radio is on loan.

The radio dance (the risky bit, see padlink.py):
  padlink.pause()  →  wifi.connect() + wait  →  POST on a worker thread
  →  wifi.disconnect()  →  padlink.resume() (re-pins ESP-NOW channel 1)

Failures are cheap: an unsent score stays queued in the file and rides
the next game over. Everything network-shaped is injected, so the logic
runs and tests under plain CPython.
"""

try:
    import json
except ImportError:
    json = None

# The arcade character wheel: UP/DOWN steps through these.
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

TABLE_SIZE = 10
ENTRY_TIMEOUT = 20.0  # seconds of picker silence before it resolves itself
# Ignore confirm presses younger than this: the fire button that killed
# you is often still being mashed when the picker opens, and a death-mash
# must not instantly submit under the previous player's initials.
MIN_CONFIRM_AGE = 1.0

# GIL donations per tick while a POST is in flight. The cold TLS
# handshake is ~350ms of CPU-bound mbedTLS; at the worker's default
# 5ms-per-tick donation (~10% duty) it can't finish inside any sane
# deadline (verified on hardware: standalone POST 532ms, same POST
# under the scheduler dead at 20s). A burst of donations is fine here:
# it's game over, the LEDs are only running the marquee.
WAIT_DONATION_BURST = 8


class InitialsEntry:
    """Three-slot arcade initials picker.

    cycle() spins the letter wheel under the cursor, move() shifts the
    cursor, confirm() locks it in. The app renders .text and .cursor.
    """

    def __init__(self, initials="AAA"):
        self.slots = []
        for i in range(3):
            ch = initials[i] if i < len(initials) else "A"
            self.slots.append(LETTERS.index(ch) if ch in LETTERS else 0)
        self.cursor = 0
        self.done = False
        self.touched = False  # any human input since the picker opened
        self.elapsed = 0.0    # since last input (drives the timeout)
        self.age = 0.0        # since the picker opened (never resets)

    @property
    def text(self):
        return "".join(LETTERS[i] for i in self.slots)

    def cycle(self, step):
        """Spin the wheel under the cursor (UP/DOWN)."""
        self.slots[self.cursor] = (self.slots[self.cursor] + step) % len(LETTERS)
        self.touched = True
        self.elapsed = 0.0

    def move(self, step):
        """Shift the cursor (LEFT/RIGHT), clamped to the three slots."""
        self.cursor = max(0, min(2, self.cursor + step))
        self.touched = True
        self.elapsed = 0.0

    def confirm(self):
        self.done = True
        return self.text

    def tick(self, delta):
        """Resolve the picker after ENTRY_TIMEOUT of silence, so a
        walked-away player never wedges the badge in entry mode.

        Returns None while waiting; the initials string if the picker
        was touched (they engaged, then wandered off - keep the score);
        or "" if nobody ever touched it (an abandoned game - the score
        must NOT go on the board under the previous player's initials)."""
        self.elapsed += delta
        self.age += delta
        if self.elapsed >= ENTRY_TIMEOUT:
            self.done = True
            return self.text if self.touched else ""
        return None


class ScoreTable:
    """Local top-N high scores plus the queue of entries not yet
    submitted to the server. One JSON file on flash holds both, so
    scores and unsent submissions survive a power cycle.

    path=None keeps everything in memory (simulator, tests)."""

    def __init__(self, path=None, size=TABLE_SIZE):
        self.path = path
        self.size = size
        self.scores = []   # [{"name","score","wave","difficulty"}] best first
        self.pending = []  # same shape, oldest first, waiting for the server
        self._load()

    def qualifies(self, score):
        """Would this score make the table?"""
        if score <= 0:
            return False
        if len(self.scores) < self.size:
            return True
        return score > self.scores[-1]["score"]

    def add(self, name, score, wave=None, difficulty=None):
        """Record a score. Returns its 1-based local rank, or None if it
        didn't make the table (it still queues for the server — the
        global board may be emptier than ours)."""
        entry = {"name": name, "score": score,
                 "wave": wave, "difficulty": difficulty}
        self.pending.append(entry)
        rank = None
        if self.qualifies(score):
            # Insert behind equal scores: first to a score ranks higher
            i = 0
            while i < len(self.scores) and self.scores[i]["score"] >= score:
                i += 1
            self.scores.insert(i, dict(entry))
            del self.scores[self.size:]
            rank = i + 1
        self._save()
        return rank

    def best(self):
        return self.scores[0] if self.scores else None

    def pop_pending(self):
        """Drop the oldest pending entry (it reached the server)."""
        if self.pending:
            self.pending.pop(0)
            self._save()

    def _load(self):
        if self.path is None or json is None:
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.scores = list(data.get("scores", []))[: self.size]
            self.pending = list(data.get("pending", []))
        except (OSError, ValueError):
            pass  # first boot, or a corrupt file: start fresh

    def _save(self):
        if self.path is None or json is None:
            return
        try:
            with open(self.path, "w") as f:
                json.dump({"scores": self.scores, "pending": self.pending}, f)
        except OSError as e:
            print("highscore: save failed: {}".format(e))


class Submitter:
    """Drives one submission session per kick(): borrow the radio, drain
    the pending queue to the server, hand the radio back.

    Everything with a side effect is injected (wifi module, padlink,
    worker, client factory), so the state machine tests under CPython.
    ``status`` is a short string for the LCD: off | idle | wifi.. |
    send.. | sent #N | no wifi | fail
    """

    def __init__(self, table, game, client, api_key,
                 wifi=None, padlink=None, worker=None,
                 wifi_timeout=15.0):
        self.table = table
        self.game = game
        self.client = client          # httpclient.PersistentClient or fake
        self.api_key = api_key
        self.wifi = wifi              # badge OS wifi module (None = offline)
        self.padlink = padlink        # EspNowPad to pause/resume (or None)
        self.worker = worker          # PollWorker (or None = post inline)
        self.wifi_timeout = wifi_timeout
        self.state = "idle"
        # "local" = scores save to flash and wait for the USB upload
        # tool; "off" = wifi missing entirely; "idle" = live submission.
        if client is None:
            self.status = "local"
        elif wifi is None:
            self.status = "off"
        else:
            self.status = "idle"
        self._timer = 0.0
        self.sent = 0  # entries delivered this power cycle (telemetry)

    @property
    def active(self):
        return self.state != "idle"

    def kick(self):
        """Start a session if there is anything to send and we can."""
        if (self.state != "idle" or not self.table.pending
                or self.wifi is None or self.client is None):
            return False
        if self.padlink is not None:
            self.padlink.pause()
        try:
            self.wifi.connect()
        except Exception as e:
            print("highscore: wifi.connect failed: {}".format(e))
            self._finish("no wifi")
            return False
        self.state = "wifi"
        self.status = "wifi.."
        self._timer = 0.0
        return True

    def poll(self, delta):
        """Call every background tick; cheap when idle."""
        if self.state == "idle":
            return
        self._timer += delta
        if self.state == "wifi":
            connected = False
            try:
                connected = self.wifi.status()
            except Exception:
                pass
            if connected:
                self._post_next()
            elif self._timer > self.wifi_timeout:
                self._finish("no wifi")
        elif self.state == "wait":
            if self.worker is not None:
                # tick() no-ops once the job lands, so the burst is free
                # in the tail; while the job runs it donates 8x5ms.
                for _ in range(WAIT_DONATION_BURST):
                    self.worker.tick()
                res = self.worker.result()
                if res is not None:
                    self._handle(res[1])
                elif not self.worker.busy:
                    # Deadline overran and the job was abandoned
                    self._handle(None)

    def _post_next(self):
        entry = self.table.pending[0]
        body = {"game": self.game, "name": entry["name"],
                "score": entry["score"], "wave": entry.get("wave"),
                "difficulty": entry.get("difficulty")}
        headers = {"X-API-Key": self.api_key} if self.api_key else None
        fn = lambda: self.client.post("/api/v1/scores", body, headers)
        self.state = "wait"
        self.status = "send.."
        self._timer = 0.0
        if self.worker is None:
            try:
                self._handle(fn())
            except Exception as e:
                print("highscore: post failed: {}".format(e))
                self._handle(None)
        elif not self.worker.submit("score", fn):
            # Mailbox busy (shouldn't happen: we own this worker) — retry
            # next tick by dropping back to the connected state.
            self.state = "wifi"

    def _handle(self, result):
        """A POST finished (or died). result is (status, body) or None."""
        if result is None or result[0] != 201:
            print("highscore: submit failed: {}".format(
                result[0] if result else "no response"))
            self._finish("fail")  # entry stays pending for next game over
            return
        self.sent += 1
        rank = None
        try:
            rank = json.loads(result[1])["entry"]["rank"]
        except (ValueError, KeyError, TypeError):
            pass
        self.table.pop_pending()
        if self.table.pending:
            self._post_next()  # still connected: drain the queue
        else:
            self._finish("sent #{}".format(rank) if rank else "sent")

    def _finish(self, status):
        """Hand the radio back whatever happened."""
        try:
            if self.client is not None:
                self.client.close()
            if self.wifi is not None:
                self.wifi.disconnect()
        except Exception:
            pass
        if self.padlink is not None:
            self.padlink.resume()
        self.state = "idle"
        self.status = status
