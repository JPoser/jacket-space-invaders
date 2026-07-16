#!/usr/bin/env python3
"""
Standalone CPython tests for the high-score feature (highscore.py):
initials entry, the local score table, and the submission state machine
with fakes for wifi/padlink/worker/HTTP client.

Run:  python3 tildagon-app/tests/test_highscore.py
  or: pytest tildagon-app/tests/
"""

import importlib.util
import json
import sys
from pathlib import Path

JACVADERS = Path(__file__).resolve().parent.parent / "JacVaders"


def load(name):
    spec = importlib.util.spec_from_file_location(name, JACVADERS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


highscore = load("highscore")
pollworker = load("pollworker")


# -- Fakes -------------------------------------------------------------------------

class FakeWifi:
    def __init__(self, connect_after=0):
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.connect_after = connect_after  # status() polls until connected
        self._polls = 0

    def connect(self):
        self.connect_calls += 1
        self._polls = 0

    def status(self):
        self._polls += 1
        return self._polls > self.connect_after

    def disconnect(self):
        self.disconnect_calls += 1


class FakePadlink:
    def __init__(self):
        self.paused = False
        self.pauses = 0
        self.resumes = 0

    def pause(self):
        self.paused = True
        self.pauses += 1

    def resume(self):
        self.paused = False
        self.resumes += 1


class FakeClient:
    """Records POSTs; scripted responses, then repeats the last one."""

    def __init__(self, responses=None):
        self.posts = []
        self.responses = list(responses or [])
        self.closed = 0

    def post(self, path, body, headers=None):
        self.posts.append((path, body, headers))
        if isinstance(self.responses[0], Exception):
            raise self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    def close(self):
        self.closed += 1


def ok_response(rank=1):
    return (201, json.dumps({"accepted": True, "entry": {"rank": rank}}))


def make_submitter(table, responses=None, connect_after=0, threaded=False):
    wifi = FakeWifi(connect_after)
    pad = FakePadlink()
    client = FakeClient(responses or [ok_response()])
    worker = pollworker.PollWorker(threaded=threaded)
    sub = highscore.Submitter(table, "jacvaders", client, "test-key",
                              wifi=wifi, padlink=pad, worker=worker,
                              wifi_timeout=5.0)
    return sub, wifi, pad, client


def run(sub, seconds=10.0, dt=0.1):
    t = 0.0
    while t < seconds and sub.active:
        sub.poll(dt)
        t += dt


# -- InitialsEntry -------------------------------------------------------------------

def test_entry_defaults_and_cycle():
    e = highscore.InitialsEntry()
    assert e.text == "AAA"
    e.cycle(+1)
    assert e.text == "BAA"
    e.cycle(-2)
    assert e.text[0] == highscore.LETTERS[-1]  # wraps backwards
    print("ok: entry cycling wraps")


def test_entry_cursor_and_confirm():
    e = highscore.InitialsEntry("JOE")
    assert e.text == "JOE"
    e.move(+1)
    e.cycle(+1)
    assert e.text == "JPE"
    e.move(-5)
    assert e.cursor == 0  # clamped
    e.move(+9)
    assert e.cursor == 2
    assert e.confirm() == "JPE"
    assert e.done
    print("ok: entry cursor + confirm")


def test_entry_timeout_autoconfirms():
    e = highscore.InitialsEntry("ZZ")
    assert e.tick(highscore.ENTRY_TIMEOUT - 1) is None
    e.cycle(+1)  # input resets the clock
    assert e.tick(highscore.ENTRY_TIMEOUT - 1) is None
    got = e.tick(2.0)
    assert got == "0ZA"  # the cycle above stepped Z onto the digits
    print("ok: entry auto-confirm timeout (input resets it)")


def test_entry_untouched_timeout_discards():
    e = highscore.InitialsEntry("CAA")
    got = e.tick(highscore.ENTRY_TIMEOUT + 1)
    assert got == ""  # nobody home: the app must drop the score
    assert e.done
    print("ok: untouched picker discards instead of ghosting the board")


def test_entry_age_never_resets():
    e = highscore.InitialsEntry()
    e.tick(2.0)
    e.cycle(+1)
    assert e.elapsed == 0.0  # input resets the walk-away clock...
    assert e.age >= 2.0      # ...but not the mash-debounce age
    print("ok: entry age survives input (confirm debounce)")


def test_entry_rubbish_initials_default():
    e = highscore.InitialsEntry("é!")
    assert e.text == "AAA"[0] * 1 + "AA"  # every slot falls back to A
    print("ok: entry survives rubbish seed initials")


# -- ScoreTable ------------------------------------------------------------------------

def test_table_add_and_rank():
    t = highscore.ScoreTable()
    assert t.add("AAA", 100) == 1
    assert t.add("BBB", 300) == 1
    assert t.add("CCC", 200) == 2
    assert [e["name"] for e in t.scores] == ["BBB", "CCC", "AAA"]
    print("ok: table insert + rank")

def test_table_ties_first_wins():
    t = highscore.ScoreTable()
    t.add("AAA", 500)
    assert t.add("BBB", 500) == 2
    assert [e["name"] for e in t.scores] == ["AAA", "BBB"]
    print("ok: table ties keep submission order")


def test_table_trims_and_qualifies():
    t = highscore.ScoreTable(size=3)
    for i, name in enumerate(("AAA", "BBB", "CCC")):
        t.add(name, (i + 1) * 100)
    assert not t.qualifies(50)
    assert not t.qualifies(100)  # equal-to-bottom doesn't make it
    assert t.qualifies(150)
    assert t.add("DDD", 50) is None      # still queued for the server
    assert len(t.scores) == 3
    assert len(t.pending) == 4
    assert t.add("EEE", 250) == 2
    assert [e["name"] for e in t.scores] == ["CCC", "EEE", "BBB"]
    print("ok: table trims to size, zero never qualifies")


def test_table_zero_scores_never_qualify():
    t = highscore.ScoreTable()
    assert not t.qualifies(0)
    print("ok: zero never qualifies")


def test_table_persists(tmp_path=None):
    import tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "scores.json")
    t = highscore.ScoreTable(path)
    t.add("JOE", 777, wave=3, difficulty="hard")
    reopened = highscore.ScoreTable(path)
    assert reopened.best()["name"] == "JOE"
    assert reopened.best()["wave"] == 3
    assert len(reopened.pending) == 1
    reopened.pop_pending()
    assert highscore.ScoreTable(path).pending == []
    print("ok: table persists scores + pending across reopen")


def test_table_survives_corrupt_file():
    import tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "scores.json")
    with open(path, "w") as f:
        f.write("{ not json")
    t = highscore.ScoreTable(path)
    assert t.scores == [] and t.pending == []
    print("ok: corrupt file starts fresh")


# -- Submitter ----------------------------------------------------------------------------

def test_submit_happy_path():
    t = highscore.ScoreTable()
    t.add("JOE", 4230, wave=7, difficulty="hard")
    sub, wifi, pad, client = make_submitter(t, [ok_response(rank=2)])
    assert sub.kick()
    assert pad.paused
    run(sub)
    assert not sub.active
    assert t.pending == []
    assert sub.status == "sent #2"
    assert wifi.connect_calls == 1 and wifi.disconnect_calls == 1
    assert pad.resumes == 1 and not pad.paused
    path, body, headers = client.posts[0]
    assert path == "/api/v1/scores"
    assert body["game"] == "jacvaders" and body["score"] == 4230
    assert body["name"] == "JOE" and body["wave"] == 7
    assert headers == {"X-API-Key": "test-key"}
    print("ok: submit happy path (pause -> wifi -> post -> resume)")


def test_submit_drains_whole_queue():
    t = highscore.ScoreTable()
    t.add("AAA", 100)
    t.add("BBB", 200)
    sub, wifi, pad, client = make_submitter(t, [ok_response(1), ok_response(1)])
    sub.kick()
    run(sub)
    assert len(client.posts) == 2
    assert t.pending == []
    assert wifi.connect_calls == 1  # one radio session for the lot
    print("ok: one session drains the whole pending queue")


def test_no_wifi_keeps_score_queued():
    t = highscore.ScoreTable()
    t.add("JOE", 100)
    sub, wifi, pad, client = make_submitter(t, connect_after=10_000)
    sub.kick()
    run(sub, seconds=30.0)
    assert not sub.active
    assert sub.status == "no wifi"
    assert len(t.pending) == 1  # still there for next game over
    assert client.posts == []
    assert pad.resumes == 1  # radio always handed back
    print("ok: wifi timeout leaves score queued, radio restored")


def test_http_failure_keeps_score_queued():
    t = highscore.ScoreTable()
    t.add("JOE", 100)
    sub, wifi, pad, client = make_submitter(t, [(500, "boom")])
    sub.kick()
    run(sub)
    assert sub.status == "fail"
    assert len(t.pending) == 1
    assert pad.resumes == 1
    # A later kick retries the same entry
    client.responses = [ok_response(1)]
    assert sub.kick()
    run(sub)
    assert t.pending == []
    print("ok: server error queues + retries on next kick")


def test_kick_noops_when_nothing_pending_or_busy():
    t = highscore.ScoreTable()
    sub, wifi, pad, client = make_submitter(t)
    assert not sub.kick()  # nothing pending
    t.add("JOE", 100)
    assert sub.kick()
    assert not sub.kick()  # already running
    print("ok: kick() no-ops when idle-with-nothing or busy")


def test_offline_submitter_stays_off():
    t = highscore.ScoreTable()
    t.add("JOE", 100)
    sub = highscore.Submitter(t, "jacvaders", None, "", wifi=None)
    assert sub.status == "off"
    assert not sub.kick()
    assert len(t.pending) == 1
    print("ok: no wifi module = local-only, scores keep queueing")


def test_threaded_worker_happy_path():
    t = highscore.ScoreTable()
    t.add("JOE", 300)
    sub, wifi, pad, client = make_submitter(t, [ok_response(1)], threaded=True)
    sub.kick()
    run(sub, seconds=20.0)
    assert not sub.active
    assert t.pending == []
    print("ok: submit via real worker thread")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                failed += 1
                print("FAIL: {}: {}".format(name, e))
    if failed:
        sys.exit("{} test(s) failed".format(failed))
    print("\nall highscore tests passed")
