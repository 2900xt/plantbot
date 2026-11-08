#!/usr/bin/env python3
"""Take a photo every second while recording; press the mouse wheel to start/stop.

    .venv/bin/python tools/wheel_capture.py
    .venv/bin/python tools/wheel_capture.py --interval 2 --out /some/folder

Press the mouse wheel down (middle click) -> start a new session folder
Press it again                           -> stop
Each session goes in Pictures_Test/<start time>/ as 0001.jpg, 0002.jpg, ...
Camera settings (device, resolution) come from config.yaml.

Runs fine next to tools/mouse_drive.py (both just read the mouse).
Stop the service first (sudo systemctl stop plantbot): it owns the camera.
On the laptop, run tools/pull_pictures.py --watch to copy photos as they are taken.
"""
from __future__ import annotations

import argparse
import os
import select
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402
from evdev import InputDevice, ecodes, list_devices  # noqa: E402

from plantbot import config  # noqa: E402
from plantbot.hal import camera  # noqa: E402


def find_mouse(name_part=None):
    """First input device with a wheel button (optionally whose name contains name_part)."""
    for path in list_devices():
        dev = InputDevice(path)
        if name_part and name_part.lower() not in dev.name.lower():
            continue
        if ecodes.BTN_MIDDLE in dev.capabilities().get(ecodes.EV_KEY, []):
            return dev
    return None


def wheel_presses(mouse, timeout):
    """Wait up to `timeout` seconds; return how many wheel presses arrived."""
    presses = 0
    if select.select([mouse.fd], [], [], max(0.0, timeout))[0]:
        for e in mouse.read():
            if e.type == ecodes.EV_KEY and e.code == ecodes.BTN_MIDDLE and e.value == 1:
                presses += 1
    return presses


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between photos")
    ap.add_argument("--out", default=str(config.ROOT / "Pictures_Test"))
    ap.add_argument("--device", help="use the mouse whose name contains this text")
    ap.add_argument("--config")
    a = ap.parse_args()

    mouse = find_mouse(a.device)
    if mouse is None:
        sys.exit("No mouse with a wheel button found. Is it plugged into the Pi?")
    cfg = config.load(a.config)
    cam, status = camera.make(cfg.camera, cfg.mock)
    print(f"Using mouse: {mouse.name}")
    print(f"camera {cfg.camera.driver}: {status}")
    print("Press the mouse wheel to start/stop taking photos. Ctrl+C to quit.")

    session, count, next_shot = None, 0, 0.0
    try:
        while True:
            wait = next_shot - time.monotonic() if session else None
            presses = wheel_presses(mouse, wait if wait is not None else 3600)
            for _ in range(presses):
                if session is None:
                    session = Path(a.out) / time.strftime("%Y-%m-%d_%H-%M-%S")
                    session.mkdir(parents=True, exist_ok=True)
                    count, next_shot = 0, time.monotonic()
                    print(f"START  saving to {session}")
                else:
                    print(f"STOP   {count} photos in {session}")
                    session = None
            if session and time.monotonic() >= next_shot:
                count += 1
                rgb = cam.capture()
                ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
                tmp = session / f".{count:04d}.jpg.tmp"  # hidden until complete, so the
                tmp.write_bytes(buf.tobytes())           # laptop puller never sees half a file
                os.replace(tmp, session / f"{count:04d}.jpg")
                print(f"  saved {count:04d}.jpg")
                next_shot = max(next_shot + a.interval, time.monotonic())  # no burst if a grab ran slow
    except KeyboardInterrupt:
        if session:
            print(f"\nSTOP   {count} photos in {session}")
    except OSError as e:  # mouse unplugged
        print(f"\nMouse disconnected ({e}). {count if session else 0} photos in the last session.")
    finally:
        cam.close()
        time.sleep(0.3)  # let the camera thread stop before exit (else OpenCV aborts noisily)


if __name__ == "__main__":
    main()
