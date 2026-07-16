"""
Background poll worker — keeps blocking HTTP off the render loop.

The server poll is a synchronous ``requests.get`` that can hold the loop
for a second or more (TLS handshakes especially). MAX_FRAME_DELTA stops
that fast-forwarding the animations, but they still *freeze* for the
duration — very visible on continuously-animating buffer effects. The
badge OS solves the same problem for its app-store downloads with
``async_helpers.unblock`` (requests.get on a ``_thread``); this is the
synchronous-app-loop flavour of that pattern:

  - one persistent worker thread (started once — no per-request spawn,
    no heap churn on a small board);
  - a one-slot job mailbox and a one-slot result mailbox. ``submit()``
    returns False while the previous job is running or undrained, and
    the caller simply retries next tick — polls are periodic, nothing
    is lost.

The worker runs only the fetch function; results are *applied* on the
main loop, so all app state changes stay single-threaded. Where
``_thread`` doesn't exist (or ``threaded=False``, as in the unit tests)
submit() degrades to running the job inline — the old blocking
behaviour, fully deterministic.
"""

import time

try:
    import _thread
except ImportError:
    _thread = None

if hasattr(time, "sleep_ms"):
    _sleep_ms = time.sleep_ms
else:
    def _sleep_ms(ms):
        time.sleep(ms / 1000.0)

if hasattr(time, "ticks_ms"):
    _ticks_ms = time.ticks_ms
    _ticks_diff = time.ticks_diff
else:
    def _ticks_ms():
        return int(time.monotonic() * 1000)

    def _ticks_diff(a, b):
        return a - b

# How long tick() blocks the main loop per tick while a fetch is in
# flight. On the badge the GIL is only released when the main thread
# blocks, and the OS scheduler's render loop never does — so without
# this donation the worker thread gets ZERO cpu and every poll starves
# (verified on hardware: a fetch that takes 1.7s standalone made no
# progress in 12s under the scheduler). 5ms once per ~50ms background
# tick is invisible next to the frame budget and lets a fetch complete
# in roughly its natural time.
GIL_DONATION_MS = 5


class PollWorker:
    # After this many deadline overruns the worker stops respawning and
    # degrades to blocking polls — each orphan holds its stack until its
    # slow-motion syscall finally returns, so don't accumulate forever.
    MAX_RESPAWNS = 8

    def __init__(self, threaded=True, stack_bytes=16384, deadline_s=None):
        self._job = None  # (tag, fn) waiting for the worker, or None
        self._res = None  # (tag, value) waiting for the main loop, or None
        self._gen = 0  # bumped on abandon; a worker whose gen is stale exits
        self._job_started = None
        self._deadline_ms = int(deadline_s * 1000) if deadline_s else None
        self._stack_bytes = stack_bytes
        self.respawns = 0
        # Optional callback fired on deadline abandon — the app uses it to
        # drop shared per-connection state (the keep-alive HTTP client) so
        # the orphaned worker never shares a socket with its replacement.
        self.on_abandon = None
        self.threaded = False
        if threaded and _thread is not None:
            self.threaded = self._spawn()

    def _spawn(self):
        try:
            try:
                # TLS wants stack headroom on MicroPython. CPython may
                # refuse small sizes — its default is already plenty.
                _thread.stack_size(self._stack_bytes)
            except (ValueError, OSError):
                pass
            _thread.start_new_thread(self._run, (self._gen,))
            return True
        except Exception as e:
            print("pollworker: threads unavailable ({}); polls will block"
                  .format(e))
            return False

    @property
    def busy(self):
        return self._job is not None or self._res is not None

    def submit(self, tag, fn):
        """Queue ``fn()`` for the worker, its return value to be picked up
        via result() tagged ``tag``. Returns False while the previous job
        is still running or its result is undrained — retry next tick."""
        if self.busy:
            return False
        if not self.threaded:
            self._res = (tag, self._call(fn))
            return True
        self._job_started = _ticks_ms()
        self._job = (tag, fn)
        return True

    def result(self):
        """Drain and return the completed (tag, value), or None."""
        res = self._res
        self._res = None
        return res

    def tick(self):
        """Call once per main-loop tick. While a job is in flight this
        blocks for GIL_DONATION_MS so the worker thread can actually run
        (see the constant's comment), and enforces the wall-clock deadline;
        otherwise it's free."""
        if not self.threaded or self._job is None:
            return
        if (self._deadline_ms is not None and self._job_started is not None
                and _ticks_diff(_ticks_ms(), self._job_started) > self._deadline_ms):
            self._abandon()
            return
        _sleep_ms(GIL_DONATION_MS)

    def _abandon(self):
        """The in-flight job overran its wall-clock deadline. On the badge
        that means its socket timeout is ticking in GIL-starved slow motion
        (a "10s" timeout can take minutes of wall time under the scheduler).
        Waiting it out means no polls meanwhile, so: orphan the worker (it
        exits on its own once the syscall returns; anything it produces is
        discarded) and spawn a fresh one so polling resumes now."""
        self._gen += 1
        self._job = None
        self._res = None
        self._job_started = None
        self.respawns += 1
        print("pollworker: job overran deadline; respawning worker (#{})"
              .format(self.respawns))
        if self.on_abandon is not None:
            try:
                self.on_abandon()
            except Exception:
                pass
        if self.respawns > self.MAX_RESPAWNS or not self._spawn():
            self.threaded = False
            print("pollworker: falling back to blocking polls")

    @staticmethod
    def _call(fn):
        try:
            return fn()
        except Exception as e:
            # Fetch functions do their own error signalling and return
            # None; this is the belt-and-braces for anything they missed —
            # the worker thread must never die.
            print("pollworker: job failed: {}".format(e))
            return None

    def _run(self, gen):
        while True:
            if self._gen != gen:
                return  # superseded after a deadline overrun — exit quietly
            job = self._job
            if job is None:
                time.sleep(0.02)
                continue
            tag, fn = job
            value = self._call(fn)
            if self._gen != gen:
                return  # finished late; a new worker owns the mailboxes now
            # Result before job-clear: busy stays True for the whole
            # lifecycle, so a fresh submit can never overwrite an
            # undrained result.
            self._res = (tag, value)
            self._job = None
