"""
JacVaders — Tildagon app that plays Space Invaders on the jacket's
WS2812 grid via the hexpansion board. No network, no server: the whole
game is synthesised on the badge (invaders.py). Left alone an autopilot
plays; a player can take the cannon from the badge D-pad or over BLE
(ble.py, Adafruit Bluefruit Connect's Control Pad).

Buttons — CONFIRM toggles between the two modes:
  attract:  UP/DOWN brightness, LEFT/RIGHT difficulty (easy/hard/
            nightmare — switching starts a fresh game)
  play:     LEFT/RIGHT move the cannon, UP or DOWN fire
  CANCEL    minimise (either mode)

BLE control works in both modes; the autopilot resumes after
config.PLAYER_IDLE_SECONDS without input.
"""

import app
from events.input import Buttons, BUTTON_TYPES

from . import ble
from . import blehost
from . import config
from . import highscore
from . import invaders
from . import padlink
from .strip import DimmableStrip

# Optional modules, kept under try/except so the app still loads in the
# simulator where some are stubs (same pattern as jacket-client).
try:
    import neopixel
except Exception:
    neopixel = None

try:
    from system.hexpansion.config import HexpansionConfig
except Exception:
    HexpansionConfig = None

try:
    import wifi  # badge OS wifi manager (saved camp credentials)
except Exception:
    wifi = None


# Layout constants — chosen to fit inside the round LCD bezel.
LCD_RADIUS = 120
RING_THICKNESS = 14
INNER_RADIUS = LCD_RADIUS - RING_THICKNESS
import math as _math
TAU = 2 * _math.pi

INVADER_GREEN = (120 / 255.0, 255 / 255.0, 60 / 255.0)
CANNON_WHITE = (1, 1, 1)

# Bluefruit round buttons 2-4 all fire (1 restarts, handled below).
_BLE_FIRE_BUTTONS = (0x32, 0x33, 0x34)


class JacVadersApp(app.App):
    def __init__(self):
        self.button_states = Buttons(self)

        # NeoPixel setup ------------------------------------------------------
        self.np = None
        self.led_init_error = ""
        if neopixel is None:
            self.led_init_error = "no neopixel module"
        elif HexpansionConfig is None:
            self.led_init_error = "no HexpansionConfig"
        else:
            try:
                hc = HexpansionConfig(config.HEXPANSION_PORT)
                pin = hc.pin[config.HEXPANSION_PIN_INDEX]
                self.np = DimmableStrip(neopixel.NeoPixel(pin, config.LED_COUNT))
                self.np.brightness = config.BRIGHTNESS
            except Exception as e:
                self.led_init_error = "LED init: {}".format(e)

        self.game = None
        if self.np is not None:
            self.game = invaders.SpaceInvaders(
                self.np,
                strip_count=config.STRIP_COUNT,
                leds_per_strip=config.LEDS_PER_STRIP,
                frame_rate=config.FRAME_RATE,
                shot_speed=config.SHOT_SPEED,
                bomb_speed=config.BOMB_SPEED,
                bomb_interval=config.BOMB_INTERVAL,
                bomb_max=config.BOMB_MAX,
                march_base=config.MARCH_BASE,
                march_min=config.MARCH_MIN,
                ufo_interval=config.UFO_INTERVAL,
                ufo_speed=config.UFO_SPEED,
                wave_speedup=config.WAVE_SPEEDUP,
                cannon_ai_interval=config.CANNON_AI_INTERVAL,
                player_idle_seconds=config.PLAYER_IDLE_SECONDS,
            )
            self.game.speed = config.GAME_SPEED
            self.game.set_difficulty(config.DIFFICULTY)

        # BLE. Constructed here, but the serve tasks can only be spawned
        # from inside the running event loop — see _background_update.
        # Two independent paths: the phone connects in to us (ble.py) and
        # we connect out to a hardware pad (blehost.py).
        self.ble = None
        self.pad = None
        self._ble_started = False
        if self.game is not None and config.BLE_ENABLED:
            self.ble = ble.BleController(config.BLE_NAME, self._on_ble_press)
        if self.game is not None and config.GAMEPAD_ENABLED:
            self.pad = blehost.HidHostGamepad(
                self._on_pad_button,
                name_prefix=config.GAMEPAD_NAME_PREFIX,
                keymap=config.GAMEPAD_KEYMAP,
                debug=config.GAMEPAD_DEBUG)
        # ESP-NOW bridge (jacket-pad-bridge): the packets are Bluefruit
        # format, so they route through the same handler as the phone.
        self.padlink = None
        if self.game is not None and config.PADLINK_ENABLED:
            self.padlink = padlink.EspNowPad(
                self._on_ble_press, channel=config.PADLINK_CHANNEL,
                force_channel=config.PADLINK_FORCE_CHANNEL)

        # High scores (config.HIGHSCORE_ENABLED): local top-10 on flash,
        # initials entry at game over, best-effort submission to
        # jacket-server. All optional; None everywhere = old behaviour.
        self.scores = None
        self.submitter = None
        self._entry = None          # InitialsEntry while collecting
        self._entry_score = None    # (score, wave, difficulty) snapshot
        self._last_initials = "AAA"  # remembered between games
        self._last_state = ""
        if self.game is not None and config.HIGHSCORE_ENABLED:
            self.scores = highscore.ScoreTable(config.HIGHSCORE_FILE)
            client = None
            worker = None
            if config.HIGHSCORE_URL and wifi is not None:
                try:
                    from . import httpclient
                    from . import pollworker
                    client = httpclient.PersistentClient(
                        config.HIGHSCORE_URL,
                        timeout=config.HIGHSCORE_HTTP_TIMEOUT)
                    # Deadline: on the badge a socket timeout ticks in
                    # GIL-starved slow motion; abandon and move on.
                    worker = pollworker.PollWorker(
                        deadline_s=config.HIGHSCORE_HTTP_TIMEOUT + 10)
                except Exception as e:
                    print("highscore: no submit path: {}".format(e))
            self.submitter = highscore.Submitter(
                self.scores, config.HIGHSCORE_GAME, client,
                config.HIGHSCORE_API_KEY, wifi=wifi, padlink=self.padlink,
                worker=worker, wifi_timeout=config.HIGHSCORE_WIFI_TIMEOUT)

        # Input flash: the LCD shows the last remote input for a moment,
        # so "is the controller getting through?" has a visible answer.
        self._input_flash = 0.0
        self._input_label = ""

        self.play_mode = False  # CONFIRM toggles D-pad control vs knobs
        self.manual_brightness = config.BRIGHTNESS
        self.last_error = ""
        print("JacVadersApp booted. led_init_error={!r}".format(self.led_init_error))

    # -- App lifecycle ----------------------------------------------------------

    def update(self, delta):
        if self._entry is not None:
            self._update_entry()
            return
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self.minimise()
            return
        if self.button_states.get(BUTTON_TYPES["CONFIRM"]):
            self.button_states.clear()
            self.play_mode = not self.play_mode
            print("play mode -> {}".format(self.play_mode))
            return
        if self.play_mode and self.game is not None:
            if self.button_states.get(BUTTON_TYPES["LEFT"]):
                self.button_states.clear()
                self.game.steer(-1)
                return
            if self.button_states.get(BUTTON_TYPES["RIGHT"]):
                self.button_states.clear()
                self.game.steer(1)
                return
            if (self.button_states.get(BUTTON_TYPES["UP"])
                    or self.button_states.get(BUTTON_TYPES["DOWN"])):
                self.button_states.clear()
                self.game.fire()
                return
            return
        if self.button_states.get(BUTTON_TYPES["UP"]):
            self.button_states.clear()
            self._adjust_brightness(+config.BRIGHTNESS_STEP)
            return
        if self.button_states.get(BUTTON_TYPES["DOWN"]):
            self.button_states.clear()
            self._adjust_brightness(-config.BRIGHTNESS_STEP)
            return
        if self.button_states.get(BUTTON_TYPES["LEFT"]):
            self.button_states.clear()
            self._cycle_difficulty(-1)
            return
        if self.button_states.get(BUTTON_TYPES["RIGHT"]):
            self.button_states.clear()
            self._cycle_difficulty(+1)
            return

    def _update_entry(self):
        """Badge buttons drive the initials picker: UP/DOWN spin the
        letter, LEFT/RIGHT move the cursor, CONFIRM (or CANCEL) locks it
        in — the score is never thrown away, so CANCEL just accepts."""
        entry = self._entry
        if (self.button_states.get(BUTTON_TYPES["CONFIRM"])
                or self.button_states.get(BUTTON_TYPES["CANCEL"])):
            self.button_states.clear()
            self._commit_initials(entry.confirm())
        elif self.button_states.get(BUTTON_TYPES["UP"]):
            self.button_states.clear()
            entry.cycle(+1)
        elif self.button_states.get(BUTTON_TYPES["DOWN"]):
            self.button_states.clear()
            entry.cycle(-1)
        elif self.button_states.get(BUTTON_TYPES["LEFT"]):
            self.button_states.clear()
            entry.move(-1)
        elif self.button_states.get(BUTTON_TYPES["RIGHT"]):
            self.button_states.clear()
            entry.move(+1)

    def background_update(self, delta):
        # The OS runs this in a bare create_task; an uncaught exception here
        # kills the game loop silently forever. Record and carry on.
        try:
            self._background_update(delta)
        except Exception as e:
            self.last_error = "bg: {}".format(e)[:60]
            print("background_update error: {}".format(e))

    def _background_update(self, delta):
        # The framework hands us delta in milliseconds; the game works in
        # seconds. Clamp so a stalled tick can't teleport the actors.
        delta = delta / 1000.0
        if delta > config.MAX_FRAME_DELTA:
            delta = config.MAX_FRAME_DELTA
        # First tick runs inside the event loop, so the BLE serve tasks can
        # be spawned from here (they can't from __init__).
        if not self._ble_started:
            self._ble_started = True
            if self.ble is not None:
                self.ble.start()
            if self.pad is not None:
                self.pad.start()
            if self.padlink is not None:
                self.padlink.start()
        if self.padlink is not None:
            self.padlink.poll()
        if self._input_flash > 0:
            self._input_flash -= delta
        if self.game is not None:
            self.game.tick(delta)
            if self.scores is not None:
                self._watch_game_over()
        if self._entry is not None:
            name = self._entry.tick(delta)  # auto-confirm on walk-away
            if name is not None:
                self._commit_initials(name)
        if self.submitter is not None:
            self.submitter.poll(delta)

    def _watch_game_over(self):
        """Catch the moment a human game ends: snapshot the score before
        the marquee auto-restarts the game, and open the initials picker."""
        state = self.game.state
        if (state == "over" and self._last_state != "over"
                and self.game.player and self.game.score > 0
                and self._entry is None):
            self._entry = highscore.InitialsEntry(self._last_initials)
            self._entry_score = (self.game.score, self.game.level,
                                 self.game.difficulty)
            print("game over, human score {} — initials time".format(
                self.game.score))
        self._last_state = state

    def _commit_initials(self, name):
        """Initials locked in: record locally, flash the result, and set
        the submitter loose on the pending queue."""
        score, wave, difficulty = self._entry_score
        self._entry = None
        self._entry_score = None
        self._last_initials = name
        rank = self.scores.add(name, score, wave, difficulty)
        self._note_input(
            "{} #{}".format(name, rank) if rank else "{} saved".format(name))
        print("highscore: {} {} (local rank {})".format(name, score, rank))
        if self.submitter is not None:
            self.submitter.kick()

    _BLE_LABELS = {0x31: "restart", 0x35: "fire", 0x36: "fire",
                   0x37: "left", 0x38: "right", 0x32: "fire",
                   0x33: "fire", 0x34: "fire"}

    def _note_input(self, label):
        self._input_flash = 0.4
        self._input_label = label

    def _on_ble_press(self, button):
        """A Control Pad press arrived from the phone or the ESP-NOW
        bridge: left/right move, up/down and buttons 2-4 fire, button 1
        restarts. Runs on the same event loop as the game tick, so no
        locking needed."""
        self._note_input(self._BLE_LABELS.get(
            button, "btn {}".format(chr(button))))
        if self.game is None:
            return
        if self._entry is not None:
            d = ble.BUTTON_DIRS.get(button)
            if d == (0, -1):
                self._entry.cycle(+1)
            elif d == (0, 1):
                self._entry.cycle(-1)
            elif d == (-1, 0):
                self._entry.move(-1)
            elif d == (1, 0):
                self._entry.move(+1)
            else:  # any fire/restart button locks the initials in
                self._commit_initials(self._entry.confirm())
            return
        d = ble.BUTTON_DIRS.get(button)
        if d == (-1, 0):
            self.game.steer(-1)
        elif d == (1, 0):
            self.game.steer(1)
        elif d is not None or button in _BLE_FIRE_BUTTONS:
            self.game.fire()
        elif button == ble.BUTTON_RESTART:
            self.game.new_game()

    _PAD_FIRE = ("up", "down", "a", "b", "x", "y")

    def _on_pad_button(self, name):
        """A button-down arrived from the hardware gamepad (blehost.py):
        left/right move the cannon, the face buttons (and D-pad up/down)
        fire, start restarts."""
        self._note_input(name)
        if self.game is None:
            return
        if self._entry is not None:
            if name == "up":
                self._entry.cycle(+1)
            elif name == "down":
                self._entry.cycle(-1)
            elif name == "left":
                self._entry.move(-1)
            elif name == "right":
                self._entry.move(+1)
            else:
                self._commit_initials(self._entry.confirm())
            return
        if name == "left":
            self.game.steer(-1)
        elif name == "right":
            self.game.steer(1)
        elif name in self._PAD_FIRE:
            self.game.fire()
        elif name == "start":
            self.game.new_game()

    # -- Button actions -----------------------------------------------------------

    def _adjust_brightness(self, step):
        new = self.manual_brightness + step
        if new < config.BRIGHTNESS_MIN:
            new = config.BRIGHTNESS_MIN
        if new > config.BRIGHTNESS_MAX:
            new = config.BRIGHTNESS_MAX
        self.manual_brightness = new
        if self.np is not None:
            self.np.brightness = new
        print("brightness {:.2f}".format(new))

    def _cycle_difficulty(self, step):
        """LEFT/RIGHT in attract mode: easy → hard → nightmare. Switching
        applies the preset and starts a fresh game."""
        if self.game is None:
            return
        names = invaders.DIFFICULTY_ORDER
        try:
            i = names.index(self.game.difficulty)
        except ValueError:
            i = 0
        name = names[(i + step) % len(names)]
        self.game.set_difficulty(name)
        print("difficulty -> {}".format(name))

    # -- LCD ------------------------------------------------------------------------

    def _draw_entry(self, ctx):
        """The arcade moment: big initials, cursor letter in green."""
        score, wave, _ = self._entry_score
        ctx.font_size = 14
        ctx.move_to(0, -25).text("NEW HIGH SCORE")
        ctx.font_size = 20
        ctx.move_to(0, -4).text("{:05d}".format(score))
        ctx.font_size = 34
        for i, ch in enumerate(self._entry.text):
            if i == self._entry.cursor:
                ctx.rgb(*INVADER_GREEN)
            else:
                ctx.rgb(1, 1, 1)
            x = (i - 1) * 28
            ctx.move_to(x, 32).text(ch)
            if i == self._entry.cursor:
                ctx.rectangle(x - 10, 48, 20, 3).fill()
        ctx.rgb(1, 1, 1)
        ctx.font_size = 10
        ctx.move_to(0, 66).text("UP/DN letter · L/R move · FIRE ok")

    _STATUS_SHORT = {"advertising": "adv", "connected": "ok", "off": "off",
                     "no BLE": "none"}

    def _short_status(self, controller):
        """Squeeze a BLE status onto the shared LCD line. blehost's are
        already short; ble.py's longer ones get abbreviated."""
        if controller is None:
            return "off"
        s = controller.status
        return self._STATUS_SHORT.get(s, s[:6])

    def draw(self, ctx):
        ctx.save()

        # Invader-green ring around a dark centre disc.
        ctx.rgb(*INVADER_GREEN)
        ctx.rectangle(-LCD_RADIUS, -LCD_RADIUS,
                      2 * LCD_RADIUS, 2 * LCD_RADIUS).fill()
        ctx.rgb(0, 0, 0)
        ctx.arc(0, 0, INNER_RADIUS, 0, TAU, 0).fill()

        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE

        ctx.rgb(*INVADER_GREEN)
        ctx.font_size = 24
        ctx.move_to(0, -60).text("JACVADERS")

        ctx.rgb(1, 1, 1)
        if self.game is None:
            ctx.font_size = 12
            ctx.move_to(0, -10).text(self.led_init_error[:28] or "no strip")
            ctx.restore()
            return

        # Remote input flash: bright, hard to miss, gone in 0.4s.
        if self._input_flash > 0:
            ctx.rgb(*INVADER_GREEN)
            ctx.font_size = 12
            ctx.move_to(0, -44).text("» {} «".format(self._input_label))
            ctx.rgb(1, 1, 1)

        # Initials picker takes over the readout until confirmed.
        if self._entry is not None:
            self._draw_entry(ctx)
            ctx.restore()
            return

        ctx.font_size = 20
        ctx.move_to(0, -25).text("{:05d}".format(self.game.score))

        ctx.font_size = 13
        ctx.move_to(0, 0).text("wave {} · {}".format(
            self.game.level, self.game.difficulty or "custom"))
        ctx.move_to(0, 18).text(self.game.mode_label())

        # Lives as little cannons.
        ctx.rgb(*CANNON_WHITE)
        n = self.game.lives
        for i in range(n):
            x = (i - (n - 1) / 2.0) * 16
            ctx.rectangle(x - 4, 38, 8, 6).fill()

        ctx.rgb(1, 1, 1)
        ctx.font_size = 11
        control = "PLAY" if self.play_mode else "attract"
        if self.game.player:
            control += "*"
        ctx.move_to(0, 60).text("{}  ph {}  pad {}  np {}".format(
            control, self._short_status(self.ble),
            self._short_status(self.pad),
            self._short_status(self.padlink)))
        line = "brt {:.2g}".format(self.manual_brightness)
        if self.submitter is not None:
            line += "  hs {}".format(self.submitter.status)
        ctx.move_to(0, 74).text(line)
        if self.last_error:
            ctx.font_size = 10
            ctx.move_to(0, 88).text(self.last_error[:28])

        ctx.restore()


__app_export__ = JacVadersApp
