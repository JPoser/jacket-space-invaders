#!/usr/bin/env python3
"""
Standalone CPython tests for the JacVaders game engine (invaders.py)
and the BLE packet parser (ble.py).

JacVaders/__init__.py imports the badge app framework, so the modules
are loaded directly by file path (they're deliberately dependency-free).

Run:  python3 tildagon-app/tests/test_invaders.py
  or: pytest tildagon-app/tests/
"""

import importlib.util
import random
import sys
from pathlib import Path

JACVADERS = Path(__file__).resolve().parent.parent / "JacVaders"


def load(name):
    spec = importlib.util.spec_from_file_location(name, JACVADERS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # so later modules can import earlier ones
    spec.loader.exec_module(mod)
    return mod


invaders = load("invaders")
ble = load("ble")
blehost = load("blehost")
padlink = load("padlink")


class FakeStrip:
    """Mimics DimmableStrip's raw read-back contract."""

    def __init__(self, n):
        self.n = n
        self._px = [(0, 0, 0)] * n
        self.writes = 0

    def __setitem__(self, i, v):
        self._px[i] = v

    def __getitem__(self, i):
        return self._px[i]

    def write(self):
        self.writes += 1


def make_game(seed=42, **kw):
    strip = FakeStrip(invaders.COLS * invaders.ROWS)
    game = invaders.SpaceInvaders(strip, invaders.COLS, invaders.ROWS,
                                  rng=random.Random(seed).random, **kw)
    return game, strip


def run(game, seconds, dt=0.05, invariant=None):
    for _ in range(int(seconds / dt)):
        game.tick(dt)
        if invariant is not None:
            invariant(game)


# -- Geometry -------------------------------------------------------------------

def test_field_dimensions():
    assert invaders.COLS == 6 and invaders.ROWS == 14
    # Formation, shields and cannon all fit the grid.
    assert invaders.FORM_START_COL + invaders.FORM_COLS <= invaders.COLS
    for c in invaders.SHIELD_COLS:
        assert 0 <= c < invaders.COLS
    assert invaders.SHIELD_ROW < invaders.INVASION_ROW <= invaders.CANNON_ROW


def test_initial_wave():
    game, _ = make_game()
    assert game.alive_count() == invaders.FORM_COLS * invaders.FORM_ROWS
    for inv in game._alive():
        c, r = game._invader_pos(inv)
        assert 0 <= c < invaders.COLS
        assert 0 <= r < invaders.SHIELD_ROW
    assert game.shields == {c: invaders.SHIELD_HEALTH
                            for c in invaders.SHIELD_COLS}


# -- The march ---------------------------------------------------------------------

def test_march_reverses_and_drops_at_edge():
    game, _ = make_game()
    game.ox = invaders.COLS - invaders.FORM_COLS  # right flank at the edge
    game.march_dir = 1
    oy = game.oy
    game._march_step()
    assert game.oy == oy + 1
    assert game.march_dir == -1
    # Next step moves left, no further drop.
    game._march_step()
    assert game.oy == oy + 1
    assert game.ox == invaders.COLS - invaders.FORM_COLS - 1


def test_march_speeds_up_as_formation_thins():
    game, _ = make_game()
    full = game._march_interval()
    for inv in list(game._alive())[:-1]:
        inv.alive = False
    assert game._march_interval() < full


def test_invasion_ends_the_game():
    game, _ = make_game()
    # Park the formation one drop above the invasion row, at the edge so
    # the next step is a drop.
    game.oy = invaders.INVASION_ROW - (invaders.FORM_ROWS - 1) - 1
    game.ox = invaders.COLS - invaders.FORM_COLS
    game.march_dir = 1
    game._march_step()
    assert game.state == "over"
    assert game.invaded
    assert game.mode_label() == "INVADED"


def test_formation_stomps_shields():
    game, _ = make_game()
    # Right flank at the edge, one drop above the shield row: the next
    # step drops the bottom rank onto SHIELD_ROW across columns 2-5,
    # which covers the shield at column 4.
    game.ox = 2
    game.oy = invaders.SHIELD_ROW - (invaders.FORM_ROWS - 1) - 1
    game.march_dir = 1
    game._march_step()
    assert game.state == "playing"  # row 11 is not yet an invasion
    assert game.shields[4] == 0
    assert game.shields[1] == invaders.SHIELD_HEALTH  # out of reach


# -- Shooting -----------------------------------------------------------------------

def bottom_invader(game):
    return max(game._alive(), key=lambda i: game._invader_pos(i)[1])


def test_shot_kills_invader_and_scores():
    game, _ = make_game()
    inv = bottom_invader(game)
    c, r = game._invader_pos(inv)
    game.shot = [c, r + 1.0]
    before = game.score
    game._tick_shot(0.2)  # 2.4 rows of travel crosses the invader
    assert not inv.alive
    assert game.score == before + invaders.SCORES[inv.fr]
    assert game.shot is None
    assert game.explosions


def test_one_shot_in_flight():
    game, _ = make_game()
    game._fire()
    assert game.shot is not None
    first = list(game.shot)
    game._fire()
    assert game.shot == first  # second trigger pull ignored


def test_shot_erodes_own_shield():
    game, _ = make_game()
    c = invaders.SHIELD_COLS[0]
    game.cannon = c
    game._fire()
    game._tick_shot(0.2)
    assert game.shields[c] == invaders.SHIELD_HEALTH - 1
    assert game.shot is None


def test_shot_hits_ufo():
    game, _ = make_game()
    game.ufo = [2.0, 1]
    game.shot = [2, 1.0]
    before = game.score
    game._tick_shot(0.2)
    assert game.ufo is None
    assert game.score == before + invaders.UFO_SCORE


def test_clearing_the_wave_advances():
    game, _ = make_game()
    for inv in list(game._alive())[:-1]:
        inv.alive = False
    inv = bottom_invader(game)
    c, r = game._invader_pos(inv)
    game.shot = [c, r + 1.0]
    game._tick_shot(0.2)
    assert game.state == "clear"
    # Step until the celebration ends and wave 2 begins; assert at that
    # moment, before the autopilot starts thinning the new formation.
    for _ in range(120):
        game.tick(0.05)
        if game.state == "playing":
            break
    assert game.state == "playing"
    assert game.level == 2
    assert game.alive_count() == invaders.FORM_COLS * invaders.FORM_ROWS


# -- Bombs ------------------------------------------------------------------------------

def test_bomb_hits_cannon():
    game, _ = make_game()
    game.bombs = [[game.cannon, invaders.CANNON_ROW - 0.5]]
    lives = game.lives
    game._tick_bombs(0.2)
    assert game.state == "dying"
    run(game, 2.0)
    assert game.lives == lives - 1
    assert game.state == "playing"
    assert game.bombs == []  # sky cleared on respawn


def test_bomb_erodes_shield():
    game, _ = make_game()
    c = invaders.SHIELD_COLS[0]
    game.bombs = [[c, invaders.SHIELD_ROW - 0.5]]
    game._tick_bombs(0.2)
    assert game.shields[c] == invaders.SHIELD_HEALTH - 1
    assert game.bombs == []


def test_shot_cancels_bomb():
    game, _ = make_game()
    game.bombs = [[3, 9.6]]
    game.shot = [3, 10.4]
    game._tick_bombs(0.2)  # bomb falls into the shot's cell
    assert game.bombs == [] or game.shot is None


def test_game_over_and_reset():
    game, _ = make_game()
    game.lives = 1
    game.bombs = [[game.cannon, invaders.CANNON_ROW - 0.5]]
    game._tick_bombs(0.2)
    run(game, 2.0)
    assert game.state == "over"
    for _ in range(120):
        game.tick(0.05)
        if game.state == "playing":
            break
    assert game.state == "playing"
    assert game.lives == 3 and game.score == 0 and game.level == 1


# -- Player control -----------------------------------------------------------------------

def test_steer_moves_cannon_and_clamps():
    game, _ = make_game()
    game.cannon = 0
    game.steer(-1)
    assert game.cannon == 0  # clamped at the wall
    game.steer(1)
    assert game.cannon == 1
    assert game.player


def test_fire_is_player_input():
    game, _ = make_game()
    assert not game.player
    game.fire()
    assert game.player
    assert game.shot is not None


def test_player_idles_back_to_ai():
    game, _ = make_game(player_idle_seconds=1.0)
    game.steer(1)
    assert game.player
    run(game, 4.0)  # death pauses pause the idle clock; leave headroom
    assert not game.player


def test_input_skips_game_over():
    game, _ = make_game()
    game.score = 500
    game.state = "over"
    game._state_timer = 4.0
    game.fire()
    assert game.state == "playing"
    assert game.score == 0 and game.lives == 3


# -- Long-run sanity -------------------------------------------------------------------------

def test_long_run_stays_sane():
    """Three simulated minutes of autopilot: everything stays on the
    grid, the score only rises within a game, and it actually shoots."""
    game, strip = make_game()
    seen = {"score": 0, "best": 0}

    def invariant(g):
        assert 0 <= g.cannon < invaders.COLS
        for b in g.bombs:
            assert 0 <= b[0] < invaders.COLS
        if g.shot is not None:
            assert 0 <= g.shot[0] < invaders.COLS
        for inv in g._alive():
            c, _ = g._invader_pos(inv)
            assert 0 <= c < invaders.COLS
        assert g.score >= seen["score"] or g.score == 0
        seen["score"] = g.score
        seen["best"] = max(seen["best"], g.score)

    run(game, 180.0, invariant=invariant)
    assert strip.writes > 0
    assert seen["best"] > 0, "autopilot never hit anything in 3 minutes"


# -- Rendering ----------------------------------------------------------------------------------

def test_render_basics():
    game, strip = make_game()
    game.tick(0.2)  # one frame past the 10fps gate
    lps = invaders.ROWS
    # Cannon is white at its cell.
    assert strip[game.cannon * lps + invaders.CANNON_ROW] == invaders.CANNON_COLOR
    # Shields are green-ish.
    for c in invaders.SHIELD_COLS:
        px = strip[c * lps + invaders.SHIELD_ROW]
        assert px[1] > px[0] and px[1] > px[2]
    # Every live invader shows its row colour.
    for inv in game._alive():
        c, r = game._invader_pos(inv)
        assert strip[c * lps + r] in (invaders.INVADER_COLORS[inv.fr],
                                      invaders.EXPLOSION_COLOR)


# -- BLE packet parser -----------------------------------------------------------------------------

def bluefruit_pkt(button, pressed=True):
    body = bytes((0x21, 0x42, button, 0x31 if pressed else 0x30))
    return body + bytes((~sum(body) & 0xFF,))


def test_parser_presses_and_releases():
    seen = []
    p = ble.PacketParser(seen.append)
    p.feed(bluefruit_pkt(0x37))            # left pressed
    p.feed(bluefruit_pkt(0x37, False))     # released — ignored
    p.feed(bluefruit_pkt(0x35))            # up (fire) pressed
    assert seen == [0x37, 0x35]


def test_parser_reassembles_and_checks():
    seen = []
    p = ble.PacketParser(seen.append)
    pkt = bluefruit_pkt(0x38)
    p.feed(b"noise" + pkt[:2])
    p.feed(pkt[2:])
    assert seen == [0x38]
    bad = bytearray(bluefruit_pkt(0x35))
    bad[4] ^= 0xFF
    p.feed(bytes(bad))
    assert seen == [0x38]


# -- BLE HID host (hardware gamepad) -----------------------------------------------

def kb_report(*usages):
    """A boot-format keyboard report holding the given HID usage IDs."""
    keys = list(usages)[:6] + [0] * (6 - len(usages))
    return bytes([0, 0] + keys)


def test_keyboard_parser_emits_press_once():
    seen = []
    p = blehost.KeyboardReportParser(seen.append)
    p.feed(kb_report(0x08))          # E (left) pressed
    p.feed(kb_report(0x08))          # still held — no repeat
    p.feed(kb_report())              # released
    p.feed(kb_report(0x08))          # pressed again
    assert seen == [0x08, 0x08]


def test_keyboard_parser_ignores_rollover_and_runts():
    seen = []
    p = blehost.KeyboardReportParser(seen.append)
    p.feed(bytes([0, 0, 1, 1, 1, 1, 1, 1]))  # rollover error
    p.feed(b"\x00")                          # runt
    assert seen == []


def test_hid_host_routes_reports_to_buttons():
    seen = []
    pad = blehost.HidHostGamepad(seen.append)
    pad._handle_report(kb_report(0x08))          # E = left
    pad._handle_report(kb_report(0x08, 0x0A))    # + G = a (fire)
    pad._handle_report(kb_report())
    pad._handle_report(kb_report(0x3A))          # unmapped — no crash
    assert seen == ["left", "a"]


def test_hid_host_survives_bad_handler():
    def boom(name):
        raise RuntimeError("handler bug")
    pad = blehost.HidHostGamepad(boom)
    pad._handle_report(kb_report(0x06))  # must not raise


# -- ESP-NOW pad link -------------------------------------------------------------

class FakeEspNow:
    """Mimics espnow.ESPNow's any()/recv() drain contract."""

    def __init__(self, messages):
        self.messages = list(messages)

    def any(self):
        return bool(self.messages)

    def recv(self, timeout_ms):
        return (b"\x10\x06\x1c\x82\x77\x8c", self.messages.pop(0))


def test_padlink_unavailable_under_cpython():
    link = padlink.EspNowPad(lambda b: None)
    assert not link.available
    assert link.start() is False
    assert link.status == "none"
    link.poll()  # no transport — must be a no-op, not a crash


def test_padlink_drains_frames_into_parser():
    seen = []
    link = padlink.EspNowPad(seen.append)
    link._e = FakeEspNow([
        bluefruit_pkt(0x37),           # left pressed
        bluefruit_pkt(0x37, False),    # released — parser drops it
        bluefruit_pkt(0x32),           # button 2 (fire) pressed
        b"junk",                       # radio noise — parser filters
    ])
    link.poll()
    assert seen == [0x37, 0x32]
    assert not link._e.messages  # everything drained in one poll


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   {}".format(name))
            except AssertionError as e:
                fails += 1
                print("FAIL {}: {}".format(name, e))
    raise SystemExit(1 if fails else 0)
