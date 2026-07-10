"""
Configuration for the JacVaders Tildagon app.

Hardware values mirror jacket-client's tildagon-app/Jacket/config.py —
same hexpansion board, same jacket. Game tuning lives here; the field
layout and character colours are the game's identity and live in
invaders.py.
"""

# --- Hexpansion --------------------------------------------------------------

# Which slot the jacket-hexpansion board is plugged into (1-6).
HEXPANSION_PORT = 2

# Zero-based index of the HS GPIO pin to use as the WS2812 data line.
# Pin index 0 == connector pin 12 == HS_F net == what the board routes to U1.
HEXPANSION_PIN_INDEX = 0

# --- LED strip ---------------------------------------------------------------

STRIP_COUNT = 6  # number of vertical strips on the jacket
LEDS_PER_STRIP = 14  # LEDs per strip
LED_COUNT = STRIP_COUNT * LEDS_PER_STRIP  # 84 total

# Physical direction of each column's data run — see jacket-client's
# config for the full story. "DDDDDD" or None = straight wiring (also
# what the badge simulator wants).
COLUMN_LAYOUT = "UDUDUD"

# --- Rendering ----------------------------------------------------------------

FRAME_RATE = 10  # fps for strip repaints

# Ceiling on the per-frame delta fed to the game, so a stalled scheduler
# tick can't teleport bombs through the cannon.
MAX_FRAME_DELTA = 0.2

# --- UX -----------------------------------------------------------------------

# Global LED brightness (0..1), adjusted at runtime via UP/DOWN (attract).
BRIGHTNESS = 0.3
BRIGHTNESS_STEP = 0.1
BRIGHTNESS_MIN = 0.05  # never zero, so it's obvious the knob works
BRIGHTNESS_MAX = 1.0

# Game clock multiplier, adjusted at runtime via LEFT/RIGHT (attract).
GAME_SPEED = 1.0
GAME_SPEED_STEP = 0.25
GAME_SPEED_MIN = 0.25
GAME_SPEED_MAX = 3.0

# --- Player control ------------------------------------------------------------

# The autopilot takes back over after this long without any input
# (badge D-pad or BLE), so the jacket always returns to attract mode.
PLAYER_IDLE_SECONDS = 10.0

# BLE phone controller (ble.py): the badge advertises the Nordic UART
# Service under BLE_NAME; Adafruit Bluefruit Connect's Controller →
# Control Pad plays — left/right move, up/down (or buttons 2-4) fire,
# button 1 restarts. OFF by default: with NimBLE active the S3's
# coexistence arbiter starves ESP-NOW receive on the idle STA, deafening
# the gamepad bridge (padlink). Pick one — phone OR bridge.
BLE_ENABLED = False
BLE_NAME = "JacVaders"

# BLE hardware gamepad (blehost.py): the badge scans for and connects TO
# a BLE HID controller (e.g. a Steam Controller with the BLE firmware).
# Off by default: scanning costs radio time shared with ESP-NOW and the
# scheduler, and the pad path is the ESP-NOW bridge below. Turn on only
# when actually using a direct BLE HID controller.
GAMEPAD_ENABLED = False
GAMEPAD_NAME_PREFIX = "8bitdo"  # scan match, besides the HID service UUID
GAMEPAD_KEYMAP = None   # None = blehost.KEY_NAMES (8BitDo keyboard-mode)
GAMEPAD_DEBUG = False   # print every HID report — for mapping a new pad

# ESP-NOW gamepad bridge (padlink.py): jacket-pad-bridge broadcasts
# Bluetooth Classic controller events (8BitDo in D mode, DualShock, ...)
# as Bluefruit packets over ESP-NOW. Channel must match the bridge
# firmware (its default is 1 — see the channel note in padlink.py).
PADLINK_ENABLED = True
PADLINK_CHANNEL = 1

# Pin the radio to PADLINK_CHANNEL, dropping any AP the badge OS joined
# (ESP-NOW is channel-locked; a badge sitting on the home AP's channel
# can't hear the bridge). The games are offline, so this costs nothing.
# Set False if some other app needs WiFi while JacVaders runs.
PADLINK_FORCE_CHANNEL = True

# --- Game tuning ----------------------------------------------------------------

SHOT_SPEED = 12.0     # rows/second, upward
BOMB_SPEED = 5.0      # rows/second, downward
BOMB_INTERVAL = 1.6   # seconds between bomb drops (shrinks per wave)
BOMB_MAX = 3          # bombs in flight at once

MARCH_BASE = 1.1      # formation step interval with everyone alive
MARCH_MIN = 0.12      # floor: the last invader's panic sprint

UFO_INTERVAL = 14.0   # seconds between UFO passes (clock spans waves)
UFO_SPEED = 4.0       # columns/second along the top row

WAVE_SPEEDUP = 0.9    # march + bomb intervals scale by this per wave

CANNON_AI_INTERVAL = 0.18  # autopilot decision cadence (move/fire)
