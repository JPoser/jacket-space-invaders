"""
JacVaders — Space Invaders for the jacket's 6×14 LED grid.

The portrait grid is the arcade cabinet: the invader formation marches
across the top (magenta squids, cyan crabs, green octopuses, top to
bottom), dropping a row and reversing at each edge and speeding up as
it thins; the red UFO slides along row 0 now and then; two shields sit
above the white cannon on the bottom row. Left alone an autopilot
plays (dodge bombs, line up, shoot); steer(-1/+1) and fire() take over
from the badge D-pad or BLE, and the AI resumes after
player_idle_seconds of silence.

Grid convention matches jacket-client: strip index = column *
leds_per_strip + row, row 0 at the top. No app dependencies — geometry
and timing arrive via the constructor — so the module runs standalone
under CPython (tildagon-app/tests/test_invaders.py).
"""

import random

COLS = 6
ROWS = 14

UFO_ROW = 0
FORM_COLS = 4       # invader formation is 4 wide ...
FORM_ROWS = 3       # ... by 3 tall
FORM_START_COL = 1
FORM_START_ROW = 1
SHIELD_ROW = 11
SHIELD_COLS = (1, 4)
SHIELD_HEALTH = 3
CANNON_ROW = 13
INVASION_ROW = 12   # an invader here means they've landed: game over

SCORES = (30, 20, 10)   # formation row 0 (top) → 2 (bottom), arcade values
UFO_SCORE = 100

# The look. Cannon white so it never argues with the green shields;
# player shots pale yellow, bombs hot orange, so the two read as
# opposite traffic at a glance.
INVADER_COLORS = (
    (255, 60, 255),   # squids (top row)
    (0, 220, 255),    # crabs
    (120, 255, 60),   # octopuses (bottom row)
)
UFO_COLOR = (255, 0, 0)
CANNON_COLOR = (255, 255, 255)
SHOT_COLOR = (255, 255, 80)
BOMB_COLOR = (255, 80, 0)
SHIELD_COLOR = (0, 180, 60)
EXPLOSION_COLOR = (255, 255, 255)
FIELD_FLASH = (10, 64, 22)    # wave-clear celebration wash
OVER_COLOR = (120, 0, 0)      # the invaders won: they glow dim red


class _Invader:
    def __init__(self, fc, fr):
        self.fc = fc    # position within the formation
        self.fr = fr
        self.alive = True


class SpaceInvaders:
    """The whole game. tick(delta) advances the simulation and repaints
    at frame_rate."""

    def __init__(self, strip, strip_count, leds_per_strip, frame_rate=10,
                 shot_speed=12.0, bomb_speed=5.0, bomb_interval=1.6,
                 bomb_max=3, march_base=1.1, march_min=0.12,
                 ufo_interval=14.0, ufo_speed=4.0, wave_speedup=0.9,
                 cannon_ai_interval=0.18, player_idle_seconds=10.0,
                 rng=None):
        if strip_count != COLS or leds_per_strip != ROWS:
            raise ValueError("field is {}x{}, strip is {}x{}".format(
                COLS, ROWS, strip_count, leds_per_strip))
        self.strip = strip
        self.leds_per_strip = leds_per_strip
        self.frame_rate = frame_rate
        self.shot_speed = shot_speed
        self.bomb_speed = bomb_speed
        self.bomb_interval = bomb_interval
        self.bomb_max = bomb_max
        self.march_base = march_base
        self.march_min = march_min
        self.ufo_interval = ufo_interval
        self.ufo_speed = ufo_speed
        self.wave_speedup = wave_speedup
        self.cannon_ai_interval = cannon_ai_interval
        self.player_idle_seconds = player_idle_seconds
        self._rng = rng or random.random

        # Player control (badge D-pad or BLE) — same contract as JacMan:
        # any input takes the wheel, the AI takes it back after
        # player_idle_seconds of silence.
        self.player = False
        self._player_idle = 0.0

        self.speed = 1.0  # user knob, scales the whole clock
        self._elapsed = 0.0
        self._frame_elapsed = 0.0
        self.new_game()

    # -- Setup / resets --------------------------------------------------------

    def new_game(self):
        self.score = 0
        self.lives = 3
        self.level = 1
        self.state = "playing"  # playing | dying | clear | over
        self._state_timer = 0.0
        self.invaded = False
        # The UFO runs on its own clock, carried across waves — otherwise
        # short waves would mean it never shows up at all.
        self._ufo_elapsed = 0.0
        self._reset_wave()

    def _reset_wave(self):
        """A fresh formation. Each wave starts a row lower (capped) and
        marches faster; shields are rebuilt."""
        self.invaders = [_Invader(fc, fr)
                         for fr in range(FORM_ROWS)
                         for fc in range(FORM_COLS)]
        self.ox = FORM_START_COL
        self.oy = FORM_START_ROW + min(self.level - 1, 3)
        self.march_dir = 1
        self._march_elapsed = 0.0
        self.shields = {c: SHIELD_HEALTH for c in SHIELD_COLS}
        self.bombs = []       # [col, row (float)] falling
        self.shot = None      # [col, row (float)] rising, at most one
        self.ufo = None       # [col (float), direction]
        self.explosions = []  # [col, row, ttl]
        self._bomb_elapsed = 0.0
        self._ai_elapsed = 0.0
        self.cannon = COLS // 2

    def _resume_after_death(self):
        """Back to playing after the death flash: the sky is cleared but
        the formation keeps whatever ground it has taken."""
        self.bombs = []
        self.shot = None
        self.explosions = []
        self.state = "playing"

    # -- Queries ----------------------------------------------------------------

    def _alive(self):
        for inv in self.invaders:
            if inv.alive:
                yield inv

    def alive_count(self):
        return sum(1 for _ in self._alive())

    def _invader_pos(self, inv):
        return (self.ox + inv.fc, self.oy + inv.fr)

    # -- Player input -------------------------------------------------------------

    def steer(self, direction):
        """Move the cannon one column (-1 left, +1 right). Steering during
        the game-over marquee starts a new game."""
        if direction not in (-1, 1):
            return
        self.player = True
        self._player_idle = 0.0
        if self.state == "over":
            self.new_game()
            return
        if self.state == "playing":
            self._move_cannon(direction)

    def fire(self):
        """Fire (one shot in flight at a time, like the arcade)."""
        self.player = True
        self._player_idle = 0.0
        if self.state == "over":
            self.new_game()
            return
        self._fire()

    def _move_cannon(self, direction):
        c = self.cannon + direction
        if 0 <= c < COLS:
            self.cannon = c

    def _fire(self):
        if self.state == "playing" and self.shot is None:
            self.shot = [self.cannon, CANNON_ROW - 1.0]

    # -- Tick ----------------------------------------------------------------------

    def tick(self, delta):
        """Advance the game and repaint if a frame is due. Returns True on
        a strip write."""
        delta *= self.speed
        self._elapsed += delta

        if self.state == "playing":
            self._tick_playing(delta)
        else:
            self._state_timer -= delta
            if self._state_timer <= 0:
                self._leave_state()

        self._frame_elapsed += delta
        if self._frame_elapsed < 1.0 / self.frame_rate:
            return False
        self._frame_elapsed = 0.0
        self._paint()
        self.strip.write()
        return True

    def _tick_playing(self, delta):
        if self.player:
            self._player_idle += delta
            if self._player_idle >= self.player_idle_seconds:
                self.player = False

        self._tick_march(delta)
        if self.state != "playing":
            return
        self._tick_bombs(delta)
        if self.state != "playing":
            return
        self._tick_shot(delta)
        if self.state != "playing":
            return
        self._tick_ufo(delta)
        self._tick_explosions(delta)

        if not self.player:
            self._ai_elapsed += delta
            if self._ai_elapsed >= self.cannon_ai_interval:
                self._ai_elapsed = 0.0
                self._ai_step()

    def _leave_state(self):
        if self.state == "dying":
            self.lives -= 1
            if self.lives <= 0:
                self.state = "over"
                self._state_timer = 4.0
            else:
                self._resume_after_death()
        elif self.state == "clear":
            self.level += 1
            self._reset_wave()
            self.state = "playing"
        elif self.state == "over":
            self.new_game()

    # -- The formation ----------------------------------------------------------------

    def _march_interval(self):
        """The heartbeat: quickens as the formation thins and per wave."""
        total = FORM_COLS * FORM_ROWS
        frac = self.alive_count() / total
        base = self.march_base * (self.wave_speedup ** (self.level - 1))
        iv = base * (0.25 + 0.75 * frac)
        return iv if iv > self.march_min else self.march_min

    def _tick_march(self, delta):
        self._march_elapsed += delta
        if self._march_elapsed < self._march_interval():
            return
        self._march_elapsed = 0.0
        self._march_step()

    def _march_step(self):
        cols = [self.ox + inv.fc for inv in self._alive()]
        if not cols:
            return
        at_edge = ((self.march_dir > 0 and max(cols) >= COLS - 1)
                   or (self.march_dir < 0 and min(cols) <= 0))
        if at_edge:
            self.oy += 1
            self.march_dir = -self.march_dir
        else:
            self.ox += self.march_dir

        for inv in self._alive():
            c, r = self._invader_pos(inv)
            if r >= INVASION_ROW:
                # They've landed. No lives-counting: it's over.
                self.invaded = True
                self.state = "over"
                self._state_timer = 4.0
                return
            if r == SHIELD_ROW and self.shields.get(c, 0) > 0:
                self.shields[c] = 0  # stomped flat

    # -- Bombs -------------------------------------------------------------------------

    def _tick_bombs(self, delta):
        # Drop a new bomb from the bottom-most invader of a random column.
        self._bomb_elapsed += delta
        interval = self.bomb_interval * (self.wave_speedup ** (self.level - 1))
        if self._bomb_elapsed >= interval:
            self._bomb_elapsed = 0.0
            if len(self.bombs) < self.bomb_max:
                bottoms = {}
                for inv in self._alive():
                    c, r = self._invader_pos(inv)
                    if c not in bottoms or r > bottoms[c]:
                        bottoms[c] = r
                if bottoms:
                    cols = sorted(bottoms)
                    c = cols[int(self._rng() * len(cols)) % len(cols)]
                    if bottoms[c] + 1 < ROWS:
                        self.bombs.append([c, bottoms[c] + 1.0])

        # Fall, checking every cell crossed so a fast frame can't tunnel.
        survivors = []
        for b in self.bombs:
            old = int(b[1])
            b[1] += delta * self.bomb_speed
            dead = False
            for r in range(old + 1, int(b[1]) + 1):
                c = b[0]
                if (self.shot is not None and self.shot[0] == c
                        and int(self.shot[1]) == r):
                    self.shot = None    # shot and bomb cancel out
                    self._explode(c, r)
                    dead = True
                    break
                if r == SHIELD_ROW and self.shields.get(c, 0) > 0:
                    self.shields[c] -= 1
                    dead = True
                    break
                if r == CANNON_ROW and c == self.cannon:
                    self._explode(c, r)
                    self.state = "dying"
                    self._state_timer = 1.5
                    dead = True
                    break
                if r > CANNON_ROW:
                    dead = True
                    break
            if not dead:
                survivors.append(b)
        self.bombs = survivors

    # -- The shot -----------------------------------------------------------------------

    def _tick_shot(self, delta):
        if self.shot is None:
            return
        old = int(self.shot[1])
        self.shot[1] -= delta * self.shot_speed
        new = int(self.shot[1]) if self.shot[1] >= 0 else -1
        r = old - 1
        while self.shot is not None and r >= new and r >= 0:
            if self._shot_hits(self.shot[0], r):
                self.shot = None
            r -= 1
        if self.shot is not None and self.shot[1] < 0:
            self.shot = None

    def _shot_hits(self, c, r):
        """Resolve the shot arriving in cell (c, r). True consumes it."""
        if self.ufo is not None and r == UFO_ROW and int(self.ufo[0] + 0.5) == c:
            self.score += UFO_SCORE
            self._explode(c, r)
            self.ufo = None
            return True
        for inv in self._alive():
            if self._invader_pos(inv) == (c, r):
                inv.alive = False
                self.score += SCORES[inv.fr]
                self._explode(c, r)
                if self.alive_count() == 0:
                    self.state = "clear"
                    self._state_timer = 2.0
                return True
        for b in self.bombs:
            if b[0] == c and int(b[1]) == r:
                self.bombs.remove(b)
                self._explode(c, r)
                return True
        if r == SHIELD_ROW and self.shields.get(c, 0) > 0:
            self.shields[c] -= 1  # your own shield blocks your shot
            return True
        return False

    # -- UFO / explosions ----------------------------------------------------------------

    def _tick_ufo(self, delta):
        if self.ufo is None:
            self._ufo_elapsed += delta
            if self._ufo_elapsed >= self.ufo_interval:
                self._ufo_elapsed = 0.0
                d = 1 if self._rng() < 0.5 else -1
                self.ufo = [0.0 if d > 0 else COLS - 1.0, d]
        else:
            self.ufo[0] += self.ufo[1] * self.ufo_speed * delta
            if self.ufo[0] < -0.5 or self.ufo[0] > COLS - 0.5:
                self.ufo = None

    def _explode(self, c, r):
        self.explosions.append([c, r, 0.25])

    def _tick_explosions(self, delta):
        keep = []
        for e in self.explosions:
            e[2] -= delta
            if e[2] > 0:
                keep.append(e)
        self.explosions = keep

    # -- Attract-mode autopilot -------------------------------------------------------------

    def _bomb_danger(self, c):
        """Rows until the nearest bomb in column c reaches the cannon, or
        None. Bombs above an intact shield don't count — the shield eats
        them."""
        best = None
        for b in self.bombs:
            if b[0] != c:
                continue
            if self.shields.get(c, 0) > 0 and b[1] < SHIELD_ROW:
                continue
            d = CANNON_ROW - b[1]
            if 0 <= d <= 5 and (best is None or d < best):
                best = d
        return best

    def _ai_target_col(self):
        """Column to line up on: the UFO when it's worth chasing, else the
        nearest bottom-most invader. Nudges off intact-shield columns so
        the autopilot doesn't chew through its own cover."""
        if self.ufo is not None and self.shot is None:
            target = int(self.ufo[0] + 0.5)
        else:
            best = None
            best_key = None
            for inv in self._alive():
                c, r = self._invader_pos(inv)
                key = (-r, abs(c - self.cannon))  # lowest first, then nearest
                if best_key is None or key < best_key:
                    best_key = key
                    best = c
            target = best
        if target is None:
            return None
        if self.shields.get(target, 0) > 0:
            for alt in (target - 1, target + 1):
                if 0 <= alt < COLS and self.shields.get(alt, 0) == 0:
                    return alt
        return target

    def _ai_step(self):
        danger = self._bomb_danger(self.cannon)
        if danger is not None and danger <= 3:
            # Dodge: prefer a bomb-free neighbour, else the one with the
            # most rows to spare.
            def dodge_key(n):
                d = self._bomb_danger(n)
                return (0, 0) if d is None else (1, -d)
            options = [n for n in (self.cannon - 1, self.cannon + 1)
                       if 0 <= n < COLS]
            options.sort(key=dodge_key)
            if options:
                self._move_cannon(options[0] - self.cannon)
            return
        target = self._ai_target_col()
        if target is None:
            return
        if target != self.cannon:
            self._move_cannon(1 if target > self.cannon else -1)
            return
        # Lined up: shoot, unless our own shield is in the way. A little
        # trigger hesitation keeps it from being metronomic.
        if (self.shot is None and self.shields.get(self.cannon, 0) == 0
                and self._rng() < 0.9):
            self._fire()

    # -- Rendering ------------------------------------------------------------------------

    def _paint(self):
        s = self.strip
        lps = self.leds_per_strip
        over = self.state == "over"

        # Wave-clear celebration: the whole field washes green on and off.
        flash_on = (self.state == "clear"
                    and int(self._elapsed * 4) % 2 == 0)
        base = FIELD_FLASH if flash_on else (0, 0, 0)
        for i in range(COLS * lps):
            s[i] = base

        # Shields fade with their health.
        for c, h in self.shields.items():
            if h > 0 and not over:
                f = h / SHIELD_HEALTH
                s[c * lps + SHIELD_ROW] = (int(SHIELD_COLOR[0] * f),
                                           int(SHIELD_COLOR[1] * f),
                                           int(SHIELD_COLOR[2] * f))

        for inv in self._alive():
            c, r = self._invader_pos(inv)
            if 0 <= c < COLS and 0 <= r < ROWS:
                s[c * lps + r] = OVER_COLOR if over else INVADER_COLORS[inv.fr]

        if over:
            return

        if self.ufo is not None:
            c = int(self.ufo[0] + 0.5)
            if 0 <= c < COLS:
                s[c * lps + UFO_ROW] = UFO_COLOR

        for b in self.bombs:
            r = int(b[1])
            if 0 <= r < ROWS:
                s[b[0] * lps + r] = BOMB_COLOR

        if self.shot is not None:
            r = int(self.shot[1])
            if 0 <= r < ROWS:
                s[self.shot[0] * lps + r] = SHOT_COLOR

        for c, r, _ in self.explosions:
            if 0 <= c < COLS and 0 <= r < ROWS:
                s[c * lps + r] = EXPLOSION_COLOR

        # Cannon last: it flashes while dying, glows steady otherwise.
        if self.state != "dying" or int(self._elapsed * 5) % 2 == 0:
            s[self.cannon * lps + CANNON_ROW] = CANNON_COLOR

    # -- LCD helpers -----------------------------------------------------------------------

    def mode_label(self):
        if self.state == "over":
            return "INVADED" if self.invaded else "GAME OVER"
        if self.state == "dying":
            return "hit!"
        if self.state == "clear":
            return "wave clear"
        return "{} invaders".format(self.alive_count())
