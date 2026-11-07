#!/usr/bin/env python3
"""Spin each motor, read each sensor, take a photo. Run this first, every time.

    python tools/hw_check.py                 # everything
    python tools/hw_check.py motors camera   # just these
    python tools/hw_check.py --mock          # check the script itself on a laptop

Stop the service first (sudo systemctl stop plantbot): it owns the camera and GPIO.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plantbot import config  # noqa: E402

RESULTS: dict[str, str] = {}


def step(name):
    def deco(fn):
        def run(cfg):
            print(f"\n=== {name} ===")
            try:
                fn(cfg)
                RESULTS[name] = "PASS"
            except Exception as e:
                print(f"FAIL: {e!r}")
                RESULTS[name] = f"FAIL: {e}"
        run.__name__ = name
        return run
    return deco


@step("i2c")
def check_i2c(cfg):
    if cfg.mock_mode:
        print("(mock) skipped")
        return
    out = subprocess.run(["i2cdetect", "-y", str(cfg.env.i2c_bus)], capture_output=True, text=True, check=True).stdout
    print(out)
    for name, addr in (("BME680", cfg.env.bme680_address), ("ADS1115", cfg.env.ads1115_address)):
        found = f" {addr:02x}" in out
        print(f"{name} @ 0x{addr:02x}: {'found' if found else 'NOT FOUND'}")


@step("motors")
def check_motors(cfg):
    from plantbot.hal import motors

    m, status = motors.make(cfg.motors)
    print(f"driver={cfg.motors.driver} status={status}")
    if status == "offline":
        raise RuntimeError("motor driver failed to init (see log above)")
    try:
        for label, l, r in (("LEFT forward", .5, 0), ("LEFT backward", -.5, 0),
                            ("RIGHT forward", 0, .5), ("RIGHT backward", 0, -.5), ("BOTH forward", .5, .5)):
            print(f"  {label} 1 s ... watch the wheels")
            t_end = time.time() + 1.0
            while time.time() < t_end:
                m.drive(l, r)  # keep feeding the watchdog
                time.sleep(0.1)
            m.stop()
            time.sleep(0.5)
        print("  If a wheel spun the wrong way, flip invert_left/invert_right in config.yaml.")
        print("  If a servo creeps when stopped, set trim_left/trim_right.")
        print("  If a DC wheel barely moves or hums, raise hbridge.min_duty in config.yaml.")
    finally:
        m.close()


@step("env")
def check_env(cfg):
    from plantbot.hal import env

    e, status = env.make(cfg.env, cfg.mock)
    print(f"status={status}")
    if all(v == "offline" for v in status.values()):
        raise RuntimeError("no env sensors")
    for _ in range(6):  # BME680 gas needs a few seconds of heater time before it's valid
        r = e.read()
        print(f"  mq2={r.mq2_v:.3f} V  gas={r.gas_ohms}  T={r.temp_c:.1f} C  RH={r.rh:.1f} %  P={r.pressure_hpa:.1f} hPa")
        time.sleep(1.0)


@step("ranger")
def check_ranger(cfg):
    from plantbot.hal import motors, ranger

    m = motors.MockMotors()
    rg, status = ranger.make(cfg.ranger, cfg.mock, m)
    print(f"status={status}  (wave your hand in front of it)")
    for _ in range(10):
        print(f"  {rg.distance_cm()} cm")
        time.sleep(0.3)


@step("fan")
def check_fan(cfg):
    from plantbot.hal import fan

    f, status = fan.make(cfg.fan)
    print(f"status={status}  fan ON for 2 s")
    f.on()
    time.sleep(2)
    f.off()


@step("camera")
def check_camera(cfg):
    import cv2

    from plantbot.hal import camera

    cam, status = camera.make(cfg.camera, cfg.mock)
    print(f"driver={cfg.camera.driver} status={status}")
    try:
        time.sleep(1.0)  # let auto-exposure settle
        rgb = cam.capture()
        path = os.path.join(config.ROOT, "hw_check.jpg")
        cv2.imwrite(path, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        print(f"  {rgb.shape[1]}x{rgb.shape[0]} saved to {path}")
        if status == "offline":
            raise RuntimeError("real camera failed; that photo is from the mock camera")
    finally:
        cam.close()


@step("classifier")
def check_classifier(cfg):
    import numpy as np

    from plantbot.vision import classify

    c, status = classify.make(cfg.classifier)
    p = c.predict(np.full((300, 300, 3), (40, 140, 40), np.uint8))
    print(f"backend={cfg.classifier.backend} status={status} -> {p.label} p_healthy={p.p_healthy:.2f} in {p.ms:.0f} ms")


CHECKS = {f.__name__: f for f in (check_i2c, check_motors, check_env, check_ranger, check_fan, check_camera, check_classifier)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("only", nargs="*", help=f"subset of: {' '.join(CHECKS)}")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--config")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
    bad = set(a.only) - set(CHECKS)
    if bad:
        ap.error(f"unknown check(s) {bad}; choose from {list(CHECKS)}")
    cfg = config.load(a.config, mock=a.mock)
    for name in a.only or CHECKS:
        CHECKS[name](cfg)
    print("\n=== summary ===")
    for k, v in RESULTS.items():
        print(f"{k:12s} {v}")
    sys.exit(0 if all(v == "PASS" for v in RESULTS.values()) else 1)


if __name__ == "__main__":
    main()
