"""Photo recorder: the mouse wheel toggles a session of one photo per second.

Press the wheel (mouse_drive.buttons.record) -> new folder <out_dir>/<YYYY-MM-DD_HH-MM-SS>/,
then 0001.jpg, 0002.jpg, ... every `interval_s`. Press again -> stop, and session.json is
written to mark the session complete (start/stop time, photo count). The laptop side
(tools/pull_pictures.py -> tools/send_to_leaflink.py) copies the folder, sends every photo
to LeafLink and opens a session report once session.json is there.

* Only fresh frames are saved: if the camera stops delivering (USB drop-out), photos are
  skipped and counted, never filled with the last good frame.
* Never records the synthetic fallback camera (devices["camera"] == "offline").
* A second press within `lockout_s` of a start/stop is ignored (switch bounce).
* Files are written under a hidden temp name and renamed, so a half-written photo is
  never visible to the copier.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2

from plantbot import config

log = logging.getLogger(__name__)


def _write_atomic(folder: Path, name: str, data: bytes) -> None:
    tmp = folder / f".{name}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, folder / name)


class Recorder:
    def __init__(self, cam, cfg, camera_status: str = "ok", jpeg_quality: int = 90):
        cfg = cfg or config.Cfg()
        self.cam = cam
        self.enabled = bool(cfg.get("enabled", True))
        self.interval = float(cfg.get("interval_s", 1.0))
        self.lockout = float(cfg.get("lockout_s", 0.3))
        self.max_age = float(cfg.get("max_frame_age_s", 1.0))
        out = Path(cfg.get("out_dir", "Pictures_Test"))
        self.out = out if out.is_absolute() else config.ROOT / out
        self.camera_status = camera_status
        self.quality = jpeg_quality
        self.session: Path | None = None
        self.started = ""
        self.count = self.skipped = 0
        self.last_toggle = float("-inf")
        self._last_frame_at = 0.0
        self._cam_lost = False
        self._wake = asyncio.Event()
        self._lock = threading.Lock()  # a photo and the stop marker never interleave

    # --- control -------------------------------------------------------------------------
    @property
    def recording(self) -> bool:
        return self.session is not None

    def toggle(self, pressed_at: float | None = None) -> str:
        """Start or stop. `pressed_at` = input event time (seconds) for the bounce lockout."""
        t = time.monotonic() if pressed_at is None else pressed_at
        if t - self.last_toggle < self.lockout:
            log.info("recorder: ignored a 2nd press %.2f s after the last one", t - self.last_toggle)
            return "ignored"
        self.last_toggle = t
        if self.recording:
            self.stop("wheel")
            return "stopped"
        return "started" if self.start() else "refused"

    def start(self) -> bool:
        if not self.enabled:
            log.warning("recorder: disabled in config (recorder.enabled)")
            return False
        if self.camera_status == "offline":
            log.error("recorder: real camera failed to open at startup; not recording synthetic images. "
                      "Check the USB camera and restart plantbot.")
            return False
        self.session = self.out / time.strftime("%Y-%m-%d_%H-%M-%S")
        self.session.mkdir(parents=True, exist_ok=True)
        self.started = datetime.now().astimezone().isoformat(timespec="seconds")
        self.count = self.skipped = 0
        self._cam_lost = False
        self._wake.set()
        log.info("recorder: START saving to %s", self.session)
        return True

    def stop(self, reason: str = "wheel") -> None:
        with self._lock:
            if not self.recording:
                return
            body = {"started": self.started, "stopped": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "photos": self.count, "skipped": self.skipped, "stopped_by": reason}
            _write_atomic(self.session, "session.json", json.dumps(body, indent=1).encode())
            extra = f", {self.skipped} skipped (camera not delivering)" if self.skipped else ""
            log.info("recorder: STOP %d photos in %s%s", self.count, self.session, extra)
            self.session = None

    def status(self) -> dict:
        return {"recording": self.recording, "session": self.session.name if self.session else None,
                "photos": self.count, "skipped": self.skipped}

    # --- capture loop --------------------------------------------------------------------
    def _fresh_frame(self):
        """RGB frame newer than the last saved one and at most max_age old, else None."""
        latest = getattr(self.cam, "latest", None)
        if latest is None:  # camera without timestamps: fall back to capture()
            return self.cam.capture()
        got = latest()
        if got is None:
            return None
        rgb, at = got
        if at <= self._last_frame_at or time.monotonic() - at > self.max_age:
            return None
        self._last_frame_at = at
        return rgb

    def _shoot(self, session: Path) -> None:
        try:
            rgb = self._fresh_frame()
        except Exception as e:  # noqa: BLE001 - a camera hiccup must not kill the loop
            log.warning("recorder: grab failed: %s", e)
            rgb = None
        ok, buf = (False, None) if rgb is None else cv2.imencode(
            ".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        with self._lock:
            if self.session is not session:  # stopped while we were grabbing: drop it
                return
            if not ok:
                self.skipped += 1
                if not self._cam_lost:
                    log.warning("recorder: CAMERA LOST - skipping photos until frames arrive again")
                    self._cam_lost = True
                return
            if self._cam_lost:
                log.info("recorder: camera back")
                self._cam_lost = False
            self.count += 1
            _write_atomic(session, f"{self.count:04d}.jpg", buf.tobytes())

    async def run(self) -> None:
        """Forever: while recording, take a photo every interval; otherwise sleep until started."""
        try:
            while True:
                if not self.recording:
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                t0 = time.monotonic()
                session = self.session
                await asyncio.to_thread(self._shoot, session)
                if self.session is not session:
                    continue
                await asyncio.sleep(max(0.0, self.interval - (time.monotonic() - t0)))
        finally:
            self.stop("shutdown")
