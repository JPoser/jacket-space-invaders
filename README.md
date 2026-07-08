# JACVADERS

Space Invaders for the LED jacket — a Tildagon badge app that drives
the jacket's 6×14 WS2812 grid via the same hexpansion board as
[jacket-client](../jacket-client), sibling to [jac-man](../jac-man).
The portrait grid *is* the arcade cabinet: invaders march across the
top, your cannon defends the hem. Left alone an autopilot plays
forever; anyone can grab the cannon from the badge D-pad or a phone
over BLE.

- **The formation**: 4×3 invaders — magenta squids (30 pts), cyan
  crabs (20), green octopuses (10), top to bottom — marching side to
  side, dropping a row and reversing at each edge, the heartbeat
  quickening as the formation thins. If they reach the bottom, they've
  landed: the field glows dim red and it's game over, no matter how
  many lives were left.
- **The cannon** is a single white LED on the bottom row. One pale
  yellow shot in flight at a time, like the arcade. Hot orange bombs
  rain down from the bottom-most invader of a random column.
- **Two green shields** sit above the cannon and fade as they erode —
  bombs chew them from above, your own shots from below, and a marching
  invader stomps them flat.
- **The red UFO** slides along the top row every so often; 100 points.
- Clear the wave and the field flashes green; the next wave starts a
  row lower and marches faster.

The badge LCD shows the score, wave, lives (as little cannon blocks),
invaders remaining, and who's driving (`PLAY*` = a human is at the
cannon right now). CONFIRM toggles between the two button modes:

- **attract**: UP/DOWN brightness, LEFT/RIGHT game speed (0.25x–3x)
- **play**: LEFT/RIGHT move the cannon, UP or DOWN fire

CANCEL minimises in either mode.

## Playing from a phone (BLE)

The badge advertises the Nordic UART Service as **JacVaders**. Install
[Adafruit Bluefruit Connect](https://learn.adafruit.com/bluefruit-le-connect)
(free, iOS/Android), connect, open **Controller → Control Pad**:
left/right move the cannon, up/down (or round buttons 2–4) fire, and
button **1** restarts. Works regardless of the badge's button mode —
the wearer can't see their own jacket, so a friend defends your back.

Any input (D-pad or BLE) puts a human in charge; the autopilot takes
back over after 10 seconds of silence (`PLAYER_IDLE_SECONDS`), so the
jacket always returns to attract mode. Input during the game-over
marquee starts a fresh game immediately.

BLE uses `aioble`, which is frozen into the Tildagon firmware. Where
it's missing (the badge simulator) the app just shows "no BLE" and the
D-pad still works.

## Layout

```
tildagon-app/
  JacVaders/
    invaders.py  the game — formation, bombs, shields, UFO, autopilot,
                 state machine, renderer (dependency-free; runs
                 standalone under CPython)
    ble.py       NUS peripheral + Bluefruit control-pad packet parser
    app.py       Tildagon app: buttons, LCD, background tick
    strip.py     DimmableStrip (brightness + serpentine remap),
                 trimmed from jacket-client's runner.py
    config.py    hexpansion port, geometry, speeds, timings
  tests/         plain-CPython tests for the engine + parser
  run_sim.sh     badge simulator with JacVaders symlinked in
  deploy.sh      upload to a real badge over USB
```

## Grid convention

Matches jacket-client: strip index = `column * LEDS_PER_STRIP + row`,
row 0 at the top of the jacket, serpentine wiring handled once in
`strip.DimmableStrip` from `config.COLUMN_LAYOUT` (`"UDUDUD"` on the
real jacket, straight in the simulator).

## Testing

```sh
python3 tildagon-app/tests/test_invaders.py   # or: pytest tildagon-app/tests/
```

The tests cover the march (edge drop/reverse, speed-up, shield stomp,
invasion), shooting (kills, scores, one-in-flight, shield erosion, UFO,
bomb cancel), bombs (cannon death, shield erosion), the full state
machine (wave clear, game over, reset), player control (move, fire,
idle fallback to AI), three simulated minutes of bounds invariants, and
the BLE packet parser.

## Simulator

Needs `badge-2024-software` cloned as a sibling of this repo, and
`jacket-client` alongside for its vendored sim patches (they teach the
simulator to render an 84-LED hexpansion strip as its own 6×14 window):

```sh
tildagon-app/run_sim.sh
# Select "JacVaders" from the app list
```

## Real badge

```sh
tildagon-app/deploy.sh                       # auto-detect port
tildagon-app/deploy.sh /dev/tty.usbmodemNNN  # explicit
```

Check `JacVaders/config.py` first: `HEXPANSION_PORT` (default 2, same
as the jacket) and `COLUMN_LAYOUT` must match how the jacket is sewn.
