#!/usr/bin/env python3
"""Collect labeled training photos from the rover camera (for whoever trains the model).

    python tools/capture_dataset.py                      # labels from models/labels.txt
    python tools/capture_dataset.py --labels healthy,sick --out dataset

Keys: 1..9 = save a photo with that label, c = toggle saving the isolated crop too, q = quit.
Saves dataset/<label>/<timestamp>.jpg (and <timestamp>_crop.jpg). Works over SSH (no GUI).
Stop the service first (sudo systemctl stop plantbot): it owns the camera.
"""
from __future__ import annotations

import argparse
import os
import sys
import termios
import time
import tty
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402

from plantbot import config  # noqa: E402
from plantbot.hal import camera  # noqa: E402
from plantbot.vision.isolate import isolate  # noqa: E402


def getch() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", help="comma-separated; default models/labels.txt")
    ap.add_argument("--out", default=str(config.ROOT / "dataset"))
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--config")
    a = ap.parse_args()
    cfg = config.load(a.config, mock=a.mock)
    labels = a.labels.split(",") if a.labels else [l.strip() for l in Path(cfg.classifier.labels).read_text().splitlines() if l.strip()]
    labels = labels[:9]
    cam, status = camera.make(cfg.camera, cfg.mock)
    print(f"camera {cfg.camera.driver}: {status}")
    for i, l in enumerate(labels, 1):
        print(f"  {i} = {l}")
    print("  c = toggle crop saving, q = quit")
    save_crop, counts = True, {l: 0 for l in labels}
    try:
        while True:
            k = getch()
            if k in ("q", "\x03"):
                break
            if k == "c":
                save_crop = not save_crop
                print(f"crop saving {'on' if save_crop else 'off'}\r")
                continue
            if not k.isdigit() or not 1 <= int(k) <= len(labels):
                continue
            label = labels[int(k) - 1]
            rgb = cam.capture()
            d = Path(a.out) / label
            d.mkdir(parents=True, exist_ok=True)
            stem = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
            cv2.imwrite(str(d / f"{stem}.jpg"), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            msg = ""
            if save_crop:
                iso = isolate(rgb, cfg.vision)
                cv2.imwrite(str(d / f"{stem}_crop.jpg"), cv2.cvtColor(iso.crop, cv2.COLOR_RGB2BGR))
                msg = f" + crop ({iso.method})"
            counts[label] += 1
            print(f"saved {label}/{stem}.jpg{msg}   counts: {counts}\r")
    finally:
        cam.close()


if __name__ == "__main__":
    main()
