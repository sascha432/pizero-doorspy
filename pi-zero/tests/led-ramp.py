#!/usr/bin/env python3
"""Power LED test: EN on once, brightness 0 -> 100 % in 10 s, hold, then brightness 0 and EN off once.

  GPIO18 (J8-12)  LED controller EN, plain output, active high - switched once on, once off
  GPIO13 (J8-33)  MOSFET gate, active high, hardware PWM1 at 10 kHz (Alt0)

Run on the Pi:  sudo python3 led-ramp.py [seconds] [gamma]   (Ctrl+C stops safely at any time)
  gamma 2.2 (default) makes the ramp look even; gamma 1 is the plain linear duty for comparison.
Works with or without the new boot config: GPIO13 is muxed to PWM1 at runtime.
"""
import os
import signal
import subprocess
import sys
import time

PWM = "/sys/class/pwm/pwmchip0"
CH = f"{PWM}/pwm1"           # GPIO13 = PWM channel 1
PERIOD = 100_000             # 10 kHz
RAMP_S = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
GAMMA = float(sys.argv[2]) if len(sys.argv) > 2 else 2.2
STEPS = 1000                 # 0.1 % brightness per step, 10 ms apart at 10 s


def w(path, val):
    with open(path, "w") as f:
        f.write(str(val))


def pinctrl(*args):
    subprocess.run(["pinctrl", "set", *args], check=True)


def duty(pct):
    """Raw duty cycle in percent (0 = off)."""
    w(f"{CH}/duty_cycle", int(PERIOD * pct / 100))


def brightness(pct):
    """Perceived brightness in percent -> duty via gamma; returns the duty used."""
    d = 100 * (pct / 100) ** GAMMA
    duty(d)
    return d


def stop(*_):
    duty(0)                       # LED off first
    pinctrl("18", "op", "dl")     # EN off - once
    w(f"{CH}/enable", 0)          # GPIO13 idles low = MOSFET off (active high)
    print("\nstopped: brightness 0, EN off")
    sys.exit(0)


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)

# 1. PWM ready at 0 % before EN comes up, so nothing flashes
if not os.path.isdir(CH):
    w(f"{PWM}/export", 1)
    time.sleep(0.5)
w(f"{CH}/enable", 0)
w(f"{CH}/duty_cycle", 0)          # duty first, never larger than the period
w(f"{CH}/period", PERIOD)
w(f"{CH}/enable", 1)
pinctrl("13", "a0")               # GPIO13 -> PWM1

# 2. EN on - once
pinctrl("18", "op", "dh")
print(subprocess.run(["pinctrl", "get", "13,18"], capture_output=True, text=True).stdout.strip())
print(f"EN on. Ramping brightness 0 -> 100 % over {RAMP_S:.0f} s at 10 kHz, gamma {GAMMA} ...")

# 3. smooth ramp, timed against the clock so it really takes RAMP_S
t0 = time.monotonic()
for i in range(STEPS + 1):
    d = brightness(100 * i / STEPS)
    if i % 100 == 0:
        print(f"  {time.monotonic() - t0:5.1f} s  brightness {100 * i // STEPS:3d} %  duty {d:6.2f} %")
    delay = t0 + RAMP_S * (i + 1) / STEPS - time.monotonic()
    if delay > 0:
        time.sleep(delay)

print("100 % - holding 3 s")
time.sleep(3)
stop()
