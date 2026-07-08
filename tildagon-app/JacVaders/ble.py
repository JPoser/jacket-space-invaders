"""
BLE gamepad link — the badge advertises the Nordic UART Service (NUS)
and a phone running Adafruit Bluefruit Connect (free, iOS/Android)
steers Pac-Man from its Controller → Control Pad screen: the D-pad
steers, round button 1 restarts the game.

Protocol: Bluefruit control-pad packets on the NUS RX characteristic,
5 bytes each — b'!' b'B' <button ascii '1'-'8'> <'1' press / '0'
release> <crc>, where crc = ~(sum of the first four bytes) & 0xFF.
Buttons 5-8 are the D-pad arrows (up/down/left/right), 1-4 the round
buttons. Packets can arrive split or coalesced; PacketParser reassembles.

The transport (aioble + bluetooth, both frozen into the Tildagon
firmware) is optional: without them — in the badge simulator, or under
CPython for the tests — the module still imports, BleController.start()
reports "no BLE", and PacketParser stays testable on its own.
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
    _UART_SERVICE = bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
    _UART_RX = bluetooth.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E")
    _UART_TX = bluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")

_ADV_INTERVAL_US = 250000

# Control-pad button numbers (ASCII) → grid directions.
BUTTON_DIRS = {
    0x35: (0, -1),   # '5' up
    0x36: (0, 1),    # '6' down
    0x37: (-1, 0),   # '7' left
    0x38: (1, 0),    # '8' right
}
BUTTON_RESTART = 0x31  # '1'

_PACKET_LEN = 5


class PacketParser:
    """Reassembles Bluefruit control-pad packets from a byte stream and
    calls on_press(button_byte) for each valid press (releases and
    checksum failures are dropped)."""

    def __init__(self, on_press):
        self.on_press = on_press
        self._buf = b""

    def feed(self, data):
        self._buf += bytes(data)
        while True:
            i = self._buf.find(b"!B")
            if i < 0:
                # Keep a trailing '!' — its 'B' may be in the next chunk.
                self._buf = self._buf[-1:] if self._buf.endswith(b"!") else b""
                return
            if len(self._buf) < i + _PACKET_LEN:
                self._buf = self._buf[i:]  # partial packet, wait for more
                return
            pkt = self._buf[i:i + _PACKET_LEN]
            self._buf = self._buf[i + _PACKET_LEN:]
            if pkt[4] != (~(pkt[0] + pkt[1] + pkt[2] + pkt[3]) & 0xFF):
                continue
            if pkt[3] == 0x31:  # '1' = pressed; ignore releases
                self.on_press(pkt[2])


class BleController:
    """NUS peripheral. start() must be called from inside the running
    event loop (any background_update tick qualifies); it spawns the
    advertise/serve task and returns immediately. ``status`` is a short
    human-readable string for the LCD."""

    def __init__(self, name, on_press):
        self.name = name
        self.status = "off"
        self._parser = PacketParser(on_press)
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

    async def _serve(self):
        try:
            service = aioble.Service(_UART_SERVICE)
            rx = aioble.Characteristic(
                service, _UART_RX,
                write=True, write_no_response=True, capture=True)
            aioble.Characteristic(service, _UART_TX, notify=True)
            aioble.register_services(service)
        except Exception as e:
            self.status = ("svc: " + str(e))[:24]
            print("ble service registration failed: {}".format(e))
            return

        while True:
            self.status = "advertising"
            try:
                connection = await aioble.advertise(
                    _ADV_INTERVAL_US, name=self.name,
                    services=[_UART_SERVICE])
            except Exception as e:
                self.status = ("adv: " + str(e))[:24]
                print("ble advertise failed: {}".format(e))
                await asyncio.sleep(5)
                continue

            self.status = "connected"
            print("ble connected: {}".format(connection.device))
            try:
                while connection.is_connected():
                    # Timeout so a silent disconnect can't hang us here.
                    try:
                        _, data = await rx.written(timeout_ms=1000)
                    except asyncio.TimeoutError:
                        continue
                    self._parser.feed(data)
            except Exception as e:
                print("ble rx error: {}".format(e))
            print("ble disconnected")
