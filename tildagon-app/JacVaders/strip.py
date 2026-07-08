"""
DimmableStrip — brightness-scaling, serpentine-aware NeoPixel wrapper.

Trimmed copy of jacket-client's runner.DimmableStrip: the game writes
full-brightness colours in logical order (column * LEDS_PER_STRIP + row,
row 0 at the top); a shadow buffer keeps the raw values, and the
serpentine remap from config.COLUMN_LAYOUT is applied only at the
hardware write, so the renderer never needs to know which way a column
is sewn. Scaling always derives from the raw shadow, so a brightness
change repaints immediately without cumulative dimming.
"""

from . import config


def _build_led_map():
    """Logical→physical LED index map from ``config.COLUMN_LAYOUT``.
    None for a straight layout so the common write path costs nothing."""
    layout = getattr(config, "COLUMN_LAYOUT", None)
    if not layout or "U" not in layout.upper():
        return None
    n = config.LEDS_PER_STRIP
    mapping = list(range(config.LED_COUNT))
    for column, direction in enumerate(layout.upper()):
        if direction == "U":
            base = column * n
            for row in range(n):
                mapping[base + row] = base + (n - 1 - row)
    return mapping


class DimmableStrip:
    def __init__(self, np):
        self._np = np
        self.n = np.n
        self._raw = [(0, 0, 0)] * np.n
        self._brightness = 1.0
        self._map = _build_led_map()

    @property
    def brightness(self):
        return self._brightness

    @brightness.setter
    def brightness(self, b):
        b = 0.0 if b < 0 else (1.0 if b > 1.0 else b)
        self._brightness = b
        if self._map is None:
            for i in range(self.n):
                self._np[i] = self._scale(self._raw[i])
        else:
            for i in range(self.n):
                self._np[self._map[i]] = self._scale(self._raw[i])
        self._np.write()

    def _scale(self, c):
        b = self._brightness
        return (int(c[0] * b), int(c[1] * b), int(c[2] * b))

    def __setitem__(self, i, v):
        self._raw[i] = v
        self._np[i if self._map is None else self._map[i]] = self._scale(v)

    def __getitem__(self, i):
        return self._raw[i]

    def __len__(self):
        return self.n

    def write(self):
        self._np.write()
