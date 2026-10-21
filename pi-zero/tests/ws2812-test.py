#!/usr/bin/env python3
"""WS2812 test on SPI0 MOSI (GPIO10, J8-19).

  python3 ws2812-test.py                 red, green, blue, white - 1 s each, repeating
  python3 ws2812-test.py --rainbow       hue cycles through the rainbow while the brightness pulses
  options: --brightness 0-255 (default 64)  --count N LEDs (default 1)  --order GRB|RGB (default GRB)
           --cycle S  rainbow hue period (default 6)  --pulse S  brightness pulse period (default 2)
Ctrl+C turns the LED(s) off and exits. Needs the spi group (no sudo).

Encoding: every WS2812 data bit becomes 3 SPI bits at 2.4 MHz (~417 ns each):
  1 -> 110 (~833 ns high), 0 -> 100 (~417 ns high). Needs a fixed core clock (core_freq_min=400).
"""
import argparse
import colorsys
import math
import signal
import sys
import time

import spidev

SPI_HZ = 2_400_000
RESET = [0] * 100            # ~330 us low: covers the 280 us reset of newer WS2812B
GAMMA = 2.2                  # same perceptual curve as the power LED

p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
p.add_argument("--rainbow", action="store_true", help="rainbow + pulsing instead of R/G/B/W steps")
p.add_argument("--brightness", type=int, default=64, help="max brightness 0-255 (default 64)")
p.add_argument("--count", type=int, default=1, help="number of LEDs in the chain")
p.add_argument("--order", default="GRB", choices=["GRB", "RGB"], help="byte order (WS2812 = GRB)")
p.add_argument("--cycle", type=float, default=6.0, help="rainbow hue period in seconds")
p.add_argument("--pulse", type=float, default=2.0, help="brightness pulse period in seconds")
args = p.parse_args()

spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = SPI_HZ
spi.mode = 0

# byte -> 3 SPI bytes (8 data bits * 3 SPI bits = 24 bits)
LUT = []
for v in range(256):
    bits = 0
    for i in range(7, -1, -1):
        bits = (bits << 3) | (0b110 if v >> i & 1 else 0b100)
    LUT.append([(bits >> 16) & 0xFF, (bits >> 8) & 0xFF, bits & 0xFF])


def show(r, g, b):
    """Set all LEDs to one colour (0-255 per channel, before gamma)."""
    r, g, b = (round(255 * (c / 255) ** GAMMA) for c in (r, g, b))
    px = (g, r, b) if args.order == "GRB" else (r, g, b)
    data = RESET[:]
    for _ in range(args.count):
        for c in px:
            data += LUT[c]
    spi.xfer2(data + RESET)


def stop(*_):
    show(0, 0, 0)
    spi.close()
    print("\noff")
    sys.exit(0)


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)

m = max(0, min(255, args.brightness))
if not args.rainbow:
    steps = [("red", (m, 0, 0)), ("green", (0, m, 0)), ("blue", (0, 0, m)), ("white", (m, m, m))]
    print(f"R/G/B/W 1 s each, brightness {m}, {args.count} LED(s), {args.order}. Ctrl+C to stop.")
    while True:
        for name, rgb in steps:
            print(f"{time.strftime('%T')}  {name:5s} {rgb}")
            show(*rgb)
            time.sleep(1)
else:
    print(f"rainbow: hue period {args.cycle} s, pulse {args.pulse} s, max {m}. Ctrl+C to stop.")
    t0 = time.monotonic()
    frame = 1 / 50            # 50 fps is smooth and cheap on the Zero
    while True:
        t = time.monotonic() - t0
        hue = (t / args.cycle) % 1.0
        level = 0.5 - 0.5 * math.cos(2 * math.pi * t / args.pulse)   # 0 -> 1 -> 0
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, level)
        show(r * m, g * m, b * m)
        time.sleep(max(0.0, frame - ((time.monotonic() - t0) - t)))
