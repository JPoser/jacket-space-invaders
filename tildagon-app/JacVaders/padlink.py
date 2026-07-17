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
same WiFi channel. If the badge OS has joined an access point, the
radio follows the AP's channel and the bridge (pinned to channel 1)
becomes inaudible — so with force_channel (the default) start()
disconnects from any AP and pins the radio. The games are offline;
they'd rather hear the controller than the internet. The status string
shows the live channel ("ok c1") so a mismatch is visible on the LCD.

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

    def __init__(self, on_press, channel=1, force_channel=True, debug=False):
        self.channel = channel
        self.force_channel = force_channel
        self.debug = debug
        self.status = "off"
        self._parser = PacketParser(on_press)
        self._e = None
        self._polls = 0  # channel-watchdog cadence counter
        self.rx_count = 0  # frames received since start (debug/telemetry)
        # While paused the poll() watchdog stands down: the high-score
        # submitter borrows the radio for WiFi, and _pin_channel would
        # otherwise drop the AP mid-POST.
        self.paused = False

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
            try:
                # MicroPython's espnow docs: reliable receive needs WiFi
                # power saving off.
                sta.config(pm=sta.PM_NONE)
            except Exception:
                pass
            if self.force_channel:
                self._pin_channel(sta)
            self._e = espnow.ESPNow()
            self._e.active(True)
            self._update_status(sta)
            return True
        except Exception as e:
            print("padlink start failed: {}".format(e))
            self.status = "err"
            return False

    def _pin_channel(self, sta):
        """Drop any AP and park the radio on our channel. The badge OS's
        WiFi manager may (re)join an AP at any time, dragging the radio
        to the AP's channel and deafening us — so this runs at start()
        AND from the poll() watchdog, winning the tug-of-war within a
        couple of seconds of any drift."""
        try:
            if sta.isconnected():
                print("padlink: dropping AP to pin channel {}".format(
                    self.channel))
                sta.disconnect()
        except Exception:
            pass
        # Re-assert PM_NONE every time, not just at start(): any
        # wifi.connect() (badge OS boot, high-score submission) re-arms
        # the default modem power save, and a sleeping radio hears ESP-NOW
        # only in bursts — on the LCD it looks like a dead gamepad.
        try:
            sta.config(pm=sta.PM_NONE)
        except Exception:
            pass
        try:
            sta.config(channel=self.channel)
        except (OSError, ValueError):
            pass  # some ports refuse mid-disconnect; status shows the truth

    def _update_status(self, sta):
        try:
            live = sta.config("channel")
        except Exception:
            self.status = "ok c?"
            return
        if live == self.channel:
            self.status = "ok c{}".format(live)
        else:
            # Audible-mismatch warning: the bridge won't be heard.
            self.status = "ch{}!={}".format(live, self.channel)

    def pause(self):
        """Lend the radio out (WiFi score submission): watchdog stands
        down and stale frames are ignored until resume()."""
        self.paused = True
        self.status = "paused"

    def resume(self):
        """Take the radio back: re-pin the channel immediately rather
        than waiting out the watchdog cadence."""
        self.paused = False
        if self._e is None:
            return
        try:
            sta = network.WLAN(network.STA_IF)
            if self.force_channel:
                self._pin_channel(sta)
            self._update_status(sta)
        except Exception as e:
            print("padlink resume failed: {}".format(e))
            self.status = "err"

    def poll(self):
        """Drain everything pending. Cheap when idle (one any() check).
        Every ~50 polls (a couple of seconds at tick rate) the channel
        watchdog re-pins the radio in case the OS moved it."""
        if self._e is None or self.paused:
            return
        self._polls += 1
        if self.force_channel and self._polls >= 50:
            self._polls = 0
            try:
                sta = network.WLAN(network.STA_IF)
                if self.debug:
                    print("padlink: ch={} connected={} status={} rx={}".format(
                        sta.config("channel"), sta.isconnected(),
                        sta.status(), self.rx_count))
                # Unconditional: _pin_channel is idempotent and also
                # re-asserts PM_NONE, which a correct-looking channel
                # can silently have lost.
                self._pin_channel(sta)
                self._update_status(sta)
            except Exception as e:
                if self.debug:
                    print("padlink watchdog err: {}".format(e))
        try:
            while self._e.any():
                _, msg = self._e.recv(0)
                if msg:
                    self.rx_count += 1
                    if self.debug:
                        print("padlink rx[{}]: {}".format(
                            self.rx_count, bytes(msg)))
                    self._parser.feed(msg)
        except Exception as e:
            print("padlink recv failed: {}".format(e))
