"""
ESP-NOW gamepad link — receives button events from jacket-pad-bridge
(an original dual-mode ESP32 running Bluepad32 that hosts Bluetooth
Classic controllers the badge's BLE-only S3 can't hear: 8BitDo pads,
DualShock, Switch Pro, ...).

The bridge broadcasts each press/release edge as a 5-byte Bluefruit
control-pad packet — the same bytes the BLE phone path (ble.py)
already parses — so this module is just the radio: activate the STA
interface, pin the channel, and drain received frames into the shared
PacketParser from the app tick. No asyncio, no pairing, no connection
state; the bridge can reboot freely and packets simply resume.

Channel note: ESP-NOW frames only arrive when both radios sit on the
same WiFi channel. An idle STA sits on PADLINK_CHANNEL (1, matching
the bridge default); if the badge ever joins an AP the radio moves to
the AP's channel, and the bridge must be rebuilt to match.

Optional transport (espnow module) with the usual guarded import: in
the simulator or under CPython the module still loads and start()
reports "none".
"""

try:
    import espnow
    import network
except Exception:
    espnow = None
    network = None

try:
    from .ble import PacketParser
except ImportError:
    # Loaded by file path (the CPython tests) rather than as a package.
    from ble import PacketParser


class EspNowPad:
    """poll() from the app tick drains pending broadcast frames into
    on_press(button_byte) — same contract as ble.BleController.
    ``status``: off | none | ok | err."""

    def __init__(self, on_press, channel=1):
        self.channel = channel
        self.status = "off"
        self._parser = PacketParser(on_press)
        self._e = None

    @property
    def available(self):
        return espnow is not None

    def start(self):
        if espnow is None:
            self.status = "none"
            return False
        try:
            sta = network.WLAN(network.STA_IF)
            sta.active(True)
            if not sta.isconnected():
                try:
                    sta.config(channel=self.channel)
                except (OSError, ValueError):
                    pass  # some ports refuse; the idle default is 1 anyway
            self._e = espnow.ESPNow()
            self._e.active(True)
            self.status = "ok"
            return True
        except Exception as e:
            print("padlink start failed: {}".format(e))
            self.status = "err"
            return False

    def poll(self):
        """Drain everything pending. Cheap when idle (one any() check)."""
        if self._e is None:
            return
        try:
            while self._e.any():
                _, msg = self._e.recv(0)
                if msg:
                    self._parser.feed(msg)
        except Exception as e:
            print("padlink recv failed: {}".format(e))
