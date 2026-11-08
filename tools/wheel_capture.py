#!/usr/bin/env python3
"""Take a photo every second while recording; press the mouse wheel to start/stop.

    .venv/bin/python tools/wheel_capture.py
    .venv/bin/python tools/wheel_capture.py --interval 2 --out /some/folder

Press the mouse wheel down (middle click) -> start a new session folder
Press it again                           -> stop
Each session goes in Pictures_Test/<start time>/ as 0001.jpg, 0002.jpg, ...
Resolution comes from config.yaml (camera.width/height).

Every saved photo is a fresh frame from the camera. If the USB camera drops out
(it can reset and come back as a different /dev/videoN), photos are skipped, not
faked, and the camera is reopened by its stable /dev/v4l/by-id name as soon as it
reappears. Never saves mock/synthetic images.

Runs fine next to tools/mouse_drive.py (both just read the mouse).
Stop the service first (sudo systemctl stop plantbot): it owns the camera.
On the laptop, run tools/pull_pictures.py --watch to copy photos as they are taken.
"""
from __future__ import annotations

import argparse
import glob
import os
import select
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402
from evdev import InputDevice, ecodes, list_devices  # noqa: E402

from plantbot import config  # noqa: E402


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


class UsbCamera:
    """USB webcam that only ever returns fresh frames and reopens itself after a drop-out."""

    def __init__(self, width, height, fallback_index=0):
        self.width, self.height, self.fallback = width, height, fallback_index
        self.cap = None
        self.path = None

    def _device(self) -> str:
        # Stable name survives the camera re-enumerating as video0 -> video1 after a USB reset.
        ids = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
        logitech = [p for p in ids if "046d" in p]
        return (logitech or ids or [f"/dev/video{self.fallback}"])[0]

    def open(self) -> bool:
        self.close()
        self.path = self._device()
        cap = cv2.VideoCapture(self.path, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000)  # don't hang if it vanishes mid-read
        if cap.isOpened() and cap.read()[0]:
            self.cap = cap
            return True
        cap.release()
        return False

    def fresh_frame(self):
        """BGR frame captured now, or None if the camera isn't working."""
        if self.cap is None and not self.open():
            return None
        self.cap.grab()  # throw away the buffered (older) frame
        ok, frame = self.cap.read()
        if not ok or frame is None:
            self.close()
            return None
        return frame

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None


def save_jpeg(folder: Path, name: str, frame) -> None:
    ok, buf = cv2.imencode(".jpg", frame)
    if not ok:
        raise RuntimeError("JPEG encode failed")
    tmp = folder / f".{name}.tmp"   # hidden until complete, so the laptop puller
    tmp.write_bytes(buf.tobytes())  # never sees half a file
    os.replace(tmp, folder / name)


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
    cam = UsbCamera(cfg.camera.width, cfg.camera.height, cfg.camera.device)
    print(f"Using mouse: {mouse.name}")
    if cam.open():
        print(f"Camera ok: {cam.path}")
    else:
        print("WARNING: camera not found yet; will keep trying (check the USB cable)")
    print("Press the mouse wheel to start/stop taking photos. Ctrl+C to quit.")

    session, count, skipped, next_shot, cam_lost = None, 0, 0, 0.0, False

    def stop_msg():
        extra = f", {skipped} skipped (camera not working)" if skipped else ""
        return f"STOP   {count} photos in {session}{extra}"

    try:
        while True:
            wait = next_shot - time.monotonic() if session else 3600
            for _ in range(wheel_presses(mouse, wait)):
                if session is None:
                    session = Path(a.out) / time.strftime("%Y-%m-%d_%H-%M-%S")
                    session.mkdir(parents=True, exist_ok=True)
                    count, skipped, next_shot = 0, 0, time.monotonic()
                    print(f"START  saving to {session}")
                else:
                    print(stop_msg())
                    session = None
            if session and time.monotonic() >= next_shot:
                frame = cam.fresh_frame()
                if frame is None:
                    skipped += 1
                    if not cam_lost:
                        print("  CAMERA LOST - skipping photos, reconnecting... (check the USB cable)")
                        cam_lost = True
                else:
                    if cam_lost:
                        print(f"  camera back ({cam.path})")
                        cam_lost = False
                    count += 1
                    save_jpeg(session, f"{count:04d}.jpg", frame)
                    print(f"  saved {count:04d}.jpg")
                next_shot = max(next_shot + a.interval, time.monotonic())  # no burst if a grab ran slow
    except KeyboardInterrupt:
        if session:
            print("\n" + stop_msg())
    except OSError as e:  # mouse unplugged
        print(f"\nMouse disconnected ({e}).")
        if session:
            print(stop_msg())
    finally:
        cam.close()


if __name__ == "__main__":
    main()
