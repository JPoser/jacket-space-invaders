"""
JacVaders — Tildagon app that plays Space Invaders on the jacket's
WS2812 grid via the hexpansion board. No network, no server: the whole
game is synthesised on the badge (invaders.py). Left alone an autopilot
plays; a player can take the cannon from the badge D-pad or over BLE
(ble.py, Adafruit Bluefruit Connect's Control Pad).

Buttons — CONFIRM toggles between the two modes:
  attract:  UP/DOWN brightness, LEFT/RIGHT game speed (0.25x–3x)
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
                self._on_ble_press, channel=config.PADLINK_CHANNEL)

        self.play_mode = False  # CONFIRM toggles D-pad control vs knobs
        self.manual_brightness = config.BRIGHTNESS
        self.last_error = ""
        print("JacVadersApp booted. led_init_error={!r}".format(self.led_init_error))

    # -- App lifecycle ----------------------------------------------------------

    def update(self, delta):
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
            self._adjust_speed(-config.GAME_SPEED_STEP)
            return
        if self.button_states.get(BUTTON_TYPES["RIGHT"]):
            self.button_states.clear()
            self._adjust_speed(+config.GAME_SPEED_STEP)
            return

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
        if self.game is not None:
            self.game.tick(delta)

    def _on_ble_press(self, button):
        """A Control Pad press arrived from the phone: left/right move,
        up/down and buttons 2-4 fire, button 1 restarts. Runs on the same
        event loop as the game tick, so no locking needed."""
        if self.game is None:
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
        if self.game is None:
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

    def _adjust_speed(self, step):
        if self.game is None:
            return
        new = self.game.speed + step
        if new < config.GAME_SPEED_MIN:
            new = config.GAME_SPEED_MIN
        if new > config.GAME_SPEED_MAX:
            new = config.GAME_SPEED_MAX
        self.game.speed = new
        print("game speed {:.2f}x".format(new))

    # -- LCD ------------------------------------------------------------------------

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

        ctx.font_size = 20
        ctx.move_to(0, -25).text("{:05d}".format(self.game.score))

        ctx.font_size = 13
        ctx.move_to(0, 0).text("wave {}".format(self.game.level))
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
        ctx.move_to(0, 74).text("spd {:.2g}x  brt {:.2g}".format(
            self.game.speed, self.manual_brightness))
        if self.last_error:
            ctx.font_size = 10
            ctx.move_to(0, 88).text(self.last_error[:28])

        ctx.restore()


__app_export__ = JacVadersApp
