#!/usr/bin/env python3
"""Look at and poke the motor driver's GPIO pins (pin numbers come from config.yaml).

    python tools/gpio_debug.py            # one snapshot of every motor pin (safe while the service runs)
    python tools/gpio_debug.py watch      # live view; click mouse buttons and watch IN1/IN2/PWM change
    python tools/gpio_debug.py toggle     # each pin high 2 s then low, one at a time: probe with a multimeter
    python tools/gpio_debug.py toggle 20  # just GPIO20

`toggle` drives the pins itself, so stop the service first: sudo systemctl stop plantbot.
Wheels can spin during `toggle` (IN1 + PWM + STBY high together); lift them off the table.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plantbot import config  # noqa: E402


def motor_pins(cfg) -> dict[int, str]:
    """GPIO number -> TB6612 pin name(s). Shared pins list every input they feed."""
    h = cfg.motors.hbridge
    pins: dict[int, list[str]] = {}
    for key, label in (("left_in1", "AIN1"), ("left_in2", "AIN2"), ("left_pwm", "PWMA"),
                       ("right_in1", "BIN1"), ("right_in2", "BIN2"), ("right_pwm", "PWMB"), ("stby", "STBY")):
        n = h.get(key)
        if n is not None:
            pins.setdefault(n, []).append(label)
    return {n: "+".join(v) for n, v in sorted(pins.items())}


def read(pins: dict[int, str]) -> dict[int, str]:
    """`pinctrl get` reads the pad level without claiming the pin, so it works while the service runs."""
    out = subprocess.run(["pinctrl", "get", ",".join(map(str, pins))], capture_output=True, text=True, check=True).stdout
    levels = {}
    for line in out.splitlines():  # "13: op -- pn | lo // GPIO13 = output"
        n, rest = line.split(":", 1)
        mode = rest.split()[0]
        level = "HI" if "| hi" in rest else "lo"
        levels[int(n)] = f"{level} ({'out' if mode == 'op' else mode})"
    return levels


def service_active() -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", "plantbot"]).returncode == 0


def show(pins: dict[int, str]) -> None:
    levels = read(pins)
    print("  ".join(f"GPIO{n} {name}={levels.get(n, '?')}" for n, name in pins.items()))


def explain(pins: dict[int, str]) -> None:
    levels = read(pins)
    hi = {name for n, name in pins.items() if levels.get(n, "").startswith("HI")}
    stby = any("STBY" in name for name in hi)
    print(f"  driver {'ENABLED' if stby else 'in STANDBY (STBY low): motors off whatever the inputs say'}")
    print("  PWM is software PWM at ~1 kHz, so a single read of a PWM pin lands on hi or lo at random;"
          " `watch` shows it flickering while a wheel runs.")


def watch(pins: dict[int, str], period: float) -> None:
    print("Ctrl-C to stop. Press mouse buttons now.")
    last = None
    try:
        while True:
            levels = read(pins)
            line = "  ".join(f"{name}={levels.get(n, '?')[:2]}" for n, name in pins.items())
            if line != last:
                print(time.strftime("%H:%M:%S"), line)
                last = line
            time.sleep(period)
    except KeyboardInterrupt:
        pass


def toggle(pins: dict[int, str], hold: float, force: bool) -> None:
    if service_active() and not force:
        sys.exit("plantbot service is running and owns these pins: sudo systemctl stop plantbot (or --force)")
    from gpiozero import DigitalOutputDevice  # lazy: only on the Pi

    for n, name in pins.items():
        with DigitalOutputDevice(n) as d:
            d.on()
            print(f"GPIO{n} ({name}) HIGH for {hold:.0f} s: expect ~3.3 V between that driver pin and GND ...")
            time.sleep(hold)
            d.off()
            print(f"GPIO{n} ({name}) low")
            time.sleep(0.5)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="status", choices=["status", "watch", "toggle"])
    ap.add_argument("pin", nargs="*", type=int, help="only these GPIO numbers")
    ap.add_argument("--config")
    ap.add_argument("--hold", type=float, default=2.0, help="toggle: seconds each pin stays high")
    ap.add_argument("--period", type=float, default=0.05, help="watch: seconds between reads")
    ap.add_argument("--force", action="store_true", help="toggle even though the service is running")
    a = ap.parse_args()

    cfg = config.load(a.config)
    pins = motor_pins(cfg)
    if a.pin:
        pins = {n: pins.get(n, "?") for n in a.pin}
    print(f"motor pins: {', '.join(f'GPIO{n}={name}' for n, name in pins.items())}")
    print(f"service: {'running (owns the pins)' if service_active() else 'stopped'}")
    if a.mode == "status":
        show(pins)
        explain(pins)
    elif a.mode == "watch":
        watch(pins, a.period)
    else:
        toggle(pins, a.hold, a.force)


if __name__ == "__main__":
    main()
