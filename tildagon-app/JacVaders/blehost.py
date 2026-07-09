"""
BLE HID host — the badge connects OUT to a hardware BLE gamepad, the
mirror image of ble.py (where the phone connects in to us).

Built for the 8BitDo Micro / Zero 2 in **keyboard mode** (hold X+power
to turn on): the pad is a BLE HID keyboard sending plain letter keys,
which dodges all vendor report-map parsing. Any BLE HID keyboard-ish
device will do; the default keymap below is the documented 8BitDo
keyboard-mode convention and can be overridden via the constructor.

Flow: scan for a device advertising the HID service (0x1812) or an
"8bitdo" name → connect → pair/bond (guarded: some devices and some
firmwares skip it) → subscribe to every notifiable Report
characteristic plus the Boot Keyboard Input Report → diff the held-key
sets and emit button-down events by name ("up", "a", "start", ...).
On disconnect it goes straight back to scanning, and a bonded pad
reconnects by itself — ideal for handing the controller around.

The transport (aioble + bluetooth) is optional: without it the module
still imports, start() reports "no BLE", and the report parsing stays
testable under CPython (tildagon-app/tests/).
"""

try:
    import asyncio
except ImportError:
    import uasyncio as asyncio

try:
    import aioble
    import bluetooth
except Exception:
    aioble = None
    bluetooth = None

if bluetooth is not None:
    _HID_SERVICE = bluetooth.UUID(0x1812)
    _REPORT_CHAR = bluetooth.UUID(0x2A4D)       # Report (input/output/feature)
    _BOOT_KB_INPUT = bluetooth.UUID(0x2A22)     # Boot Keyboard Input Report

_SCAN_MS = 15000
_SCAN_IDLE_S = 3      # breather between empty scans — don't hog the radio
_CONNECT_MS = 10000
_NOTIFY_PROP = 0x10  # GATT characteristic property bit

# ~9% scan duty cycle. A gamepad in pairing mode advertises several
# times a second, so a 30ms window every 320ms still catches it within
# a scan round — and the radio stays free for ESP-NOW and the display's
# share of the schedule. (The original 30/30 continuous scan starved
# the whole badge.)
_SCAN_INTERVAL_US = 320000
_SCAN_WINDOW_US = 30000

# 8BitDo keyboard-mode convention (Zero 2 / Micro): the pad types the
# letters C..O. Keys are HID usage IDs ('a' = 0x04).
KEY_NAMES = {
    0x06: "up",      # C
    0x07: "down",    # D
    0x08: "left",    # E
    0x09: "right",   # F
    0x0A: "a",       # G
    0x0D: "b",       # J
    0x0B: "x",       # H
    0x0C: "y",       # I
    0x0E: "l",       # K
    0x10: "r",       # M
    0x11: "select",  # N
    0x12: "start",   # O
}


class KeyboardReportParser:
    """Diffs consecutive boot-format keyboard reports ([modifiers,
    reserved, key1..key6]) and calls on_press(usage_id) once per
    key-down. Rollover-error (0x01) and empty slots are ignored;
    releases just update the held set."""

    def __init__(self, on_press):
        self.on_press = on_press
        self._held = set()

    def feed(self, data):
        if len(data) < 3:
            return
        keys = set(b for b in data[2:8] if b > 1)
        for k in keys - self._held:
            self.on_press(k)
        self._held = keys


class HidHostGamepad:
    """BLE central + HID-over-GATT client. start() must be called from
    inside the running event loop; it spawns the scan/connect/serve task
    and returns immediately. ``status`` is a short string for the LCD:
    off | no BLE | scan | conn | pair | ok | err."""

    def __init__(self, on_button, name_prefix="8bitdo", keymap=None,
                 debug=False):
        self.on_button = on_button
        self.name_prefix = name_prefix.lower()
        self.keymap = keymap or KEY_NAMES
        self.debug = debug
        self.status = "off"
        self._parser = KeyboardReportParser(self._on_key)
        self._task = None

    @property
    def available(self):
        return aioble is not None

    def start(self):
        if aioble is None:
            self.status = "no BLE"
            return False
        if self._task is None:
            self._task = asyncio.create_task(self._serve())
        return True

    # -- Report handling (transport-free, tested under CPython) ---------------

    def _handle_report(self, data):
        if self.debug:
            print("pad report: {}".format(bytes(data)))
        self._parser.feed(bytes(data))

    def _on_key(self, usage):
        name = self.keymap.get(usage)
        if name is None:
            # Unknown key: printed even outside debug, so mapping a new
            # controller is just "press everything, read the console".
            print("pad: unmapped HID usage 0x{:02x}".format(usage))
            return
        try:
            self.on_button(name)
        except Exception as e:
            print("pad button handler failed: {}".format(e))

    # -- Transport ----------------------------------------------------------------

    def _wanted(self, result):
        """Is this scan result the gamepad? HID service in the advert, or
        a name match."""
        try:
            for svc in result.services():
                if svc == _HID_SERVICE:
                    return True
        except Exception:
            pass
        name = ""
        try:
            name = result.name() or ""
        except Exception:
            pass
        return name.lower().startswith(self.name_prefix)

    async def _scan(self):
        async with aioble.scan(_SCAN_MS, interval_us=_SCAN_INTERVAL_US,
                               window_us=_SCAN_WINDOW_US,
                               active=True) as scanner:
            async for result in scanner:
                if self._wanted(result):
                    return result.device
        return None

    async def _serve(self):
        while True:
            try:
                self.status = "scan"
                device = await self._scan()
                if device is None:
                    await asyncio.sleep(_SCAN_IDLE_S)
                    continue
                self.status = "conn"
                connection = await device.connect(timeout_ms=_CONNECT_MS)
            except Exception as e:
                self.status = "err"
                print("pad connect failed: {}".format(e))
                await asyncio.sleep(2)
                continue

            try:
                await self._session(connection)
            except Exception as e:
                print("pad session ended: {}".format(e))
            finally:
                try:
                    await connection.disconnect()
                except Exception:
                    pass
            # Straight back to scanning; a bonded pad re-appears fast.

    async def _session(self, connection):
        # HOGP formally requires encryption; some pads shrug it off, and
        # older aioble builds lack pair(). Failing here is survivable —
        # try the subscription anyway and let the pad decide.
        if hasattr(connection, "pair"):
            self.status = "pair"
            try:
                await connection.pair(bond=True, le_secure=True, mitm=False,
                                      timeout_ms=20000)
            except Exception as e:
                print("pad pairing failed ({}), trying unencrypted".format(e))

        hid = await connection.service(_HID_SERVICE)
        if hid is None:
            raise OSError("no HID service")

        pumps = []
        async for char in hid.characteristics():
            if char.uuid not in (_REPORT_CHAR, _BOOT_KB_INPUT):
                continue
            props = getattr(char, "properties", _NOTIFY_PROP)
            if not props & _NOTIFY_PROP:
                continue
            try:
                await char.subscribe(notify=True)
            except Exception as e:
                print("pad subscribe failed on {}: {}".format(char.uuid, e))
                continue
            pumps.append(asyncio.create_task(self._pump(char)))
        if not pumps:
            raise OSError("no notifiable HID reports")

        self.status = "ok"
        print("pad connected: {} report source(s)".format(len(pumps)))
        try:
            await connection.disconnected(timeout_ms=None)
        finally:
            for t in pumps:
                t.cancel()
            self._parser._held = set()  # keys can't stay held across pads

    async def _pump(self, char):
        try:
            while True:
                data = await char.notified()
                self._handle_report(data)
        except Exception:
            return  # disconnect (or cancellation) lands here; _serve rescans
