"""Cameras. Each runs a background thread that keeps the latest frame and a lores JPEG."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

log = logging.getLogger(__name__)


class Camera(Protocol):
    def lores_jpeg(self) -> bytes: ...  # latest preview frame for the stream
    def capture(self) -> np.ndarray: ...  # full-res RGB uint8 still


def encode_jpeg(rgb: np.ndarray, quality: int = 80) -> bytes:
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""


def placeholder(w: int, h: int, text: str) -> np.ndarray:
    img = np.full((h, w, 3), 40, np.uint8)
    cv2.putText(img, text, (20, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (220, 220, 220), 2)
    return img


class _ThreadedCamera:
    """Subclasses implement _grab() -> RGB frame (full res). Thread keeps latest + lores JPEG."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.frame: np.ndarray | None = None
        self.jpeg = encode_jpeg(placeholder(cfg.lores_width, cfg.lores_height, "camera starting"))
        self._running = True
        self.thread = threading.Thread(target=self._loop, name="camera", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _lores(self, rgb: np.ndarray) -> np.ndarray:
        return cv2.resize(rgb, (self.cfg.lores_width, self.cfg.lores_height), interpolation=cv2.INTER_AREA)

    def _loop(self) -> None:
        period = 1.0 / self.cfg.stream_fps
        while self._running:
            t0 = time.monotonic()
            try:
                rgb = self._grab()
                jpeg = encode_jpeg(self._lores(rgb), self.cfg.jpeg_quality)
                with self.lock:
                    self.frame, self.jpeg = rgb, jpeg
            except Exception as e:
                log.warning("camera grab failed: %s", e)
                time.sleep(0.5)
            time.sleep(max(0.0, period - (time.monotonic() - t0)))

    def lores_jpeg(self) -> bytes:
        with self.lock:
            return self.jpeg

    def capture(self) -> np.ndarray:
        for _ in range(50):
            with self.lock:
                if self.frame is not None:
                    return self.frame.copy()
            time.sleep(0.05)
        raise RuntimeError("no camera frame")

    def close(self) -> None:
        self._running = False


class OpenCVCamera(_ThreadedCamera):
    """USB webcam (rover's Logitech, or a laptop webcam in mock mode)."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.cap = cv2.VideoCapture(cfg.device)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok, _ = self.cap.read()
        if not ok:
            raise RuntimeError(f"cannot read from /dev/video{cfg.device}")

    def _grab(self) -> np.ndarray:
        ok, bgr = self.cap.read()
        if not ok:
            raise RuntimeError("read failed")
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    def close(self) -> None:
        super().close()
        time.sleep(0.2)
        self.cap.release()


class PiCamera(_ThreadedCamera):
    """Pi Camera Module on CSI via picamera2 (apt install python3-picamera2)."""

    def __init__(self, cfg):
        super().__init__(cfg)
        from picamera2 import Picamera2  # lazy

        self.cam = Picamera2()
        conf = self.cam.create_video_configuration(
            main={"size": (cfg.width, cfg.height), "format": "RGB888"},
            lores={"size": (cfg.lores_width, cfg.lores_height), "format": "YUV420"},
        )
        self.cam.configure(conf)
        self.cam.start()

    def _grab(self) -> np.ndarray:
        # picamera2 "RGB888" is BGR byte order in memory.
        return cv2.cvtColor(self.cam.capture_array("main"), cv2.COLOR_BGR2RGB)

    def close(self) -> None:
        super().close()
        time.sleep(0.2)
        self.cam.stop()


class FolderCamera(_ThreadedCamera):
    """Mock camera: shows one photo from a folder; each capture() advances to the next.

    If the folder has no images it synthesizes plant photos (green leaves, some with
    yellow/brown lesions, blurred background) so mock mode needs zero assets.
    """

    EXTS = {".jpg", ".jpeg", ".png", ".bmp"}

    def __init__(self, cfg, folder: str):
        super().__init__(cfg)
        self.paths = sorted(p for p in Path(folder).glob("*") if p.suffix.lower() in self.EXTS)
        self.idx = 0
        self.current = self._load(0)
        log.info("FolderCamera: %s", f"{len(self.paths)} photos in {folder}" if self.paths else "synthetic plants")

    def _load(self, i: int) -> np.ndarray:
        if self.paths:
            bgr = cv2.imread(str(self.paths[i % len(self.paths)]))
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            return cv2.resize(rgb, (self.cfg.width, self.cfg.height))
        return synth_plant(self.cfg.width, self.cfg.height, seed=i)

    def _grab(self) -> np.ndarray:
        return self.current

    def _lores(self, rgb: np.ndarray) -> np.ndarray:
        img = super()._lores(rgb).copy()
        cv2.putText(img, f"MOCK #{self.idx}  {time.strftime('%H:%M:%S')}", (10, img.shape[0] - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        return img

    def capture(self) -> np.ndarray:
        frame = self.current.copy()
        self.idx += 1
        self.current = self._load(self.idx)
        return frame


def synth_plant(w: int, h: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = (186, 182, 174)  # table
    img[int(h * 0.75):] = (120, 95, 70)  # pot/soil band
    # Background plants: green, then blurred hard (out of focus)
    for _ in range(6):
        c = (int(rng.integers(20, 60)), int(rng.integers(90, 140)), int(rng.integers(20, 60)))
        cv2.ellipse(img, (int(rng.integers(0, w)), int(rng.integers(0, h // 2))),
                    (int(rng.integers(40, 90)), int(rng.integers(20, 40))), float(rng.integers(0, 180)),
                    0, 360, c, -1)
    img = cv2.GaussianBlur(img, (0, 0), 12)
    # Near plant: sharp leaves around the center
    sick = seed % 3  # 0 healthy, 1 some spots, 2 lots of yellowing
    cx, cy = w // 2 + int(rng.integers(-w // 10, w // 10)), h // 2
    leaf = np.zeros_like(img)
    for _ in range(9):
        ang = float(rng.uniform(0, 360))
        dx, dy = int(np.cos(np.radians(ang)) * w * 0.12), int(np.sin(np.radians(ang)) * h * 0.15)
        g = int(rng.integers(120, 190))
        col = (int(rng.integers(30, 70)), g, int(rng.integers(20, 50)))
        if sick == 2 and rng.random() < 0.5:
            col = (200, 190, 60)  # yellowing leaf
        cv2.ellipse(leaf, (cx + dx, cy + dy), (int(w * 0.09), int(h * 0.05)), ang, 0, 360, col, -1)
        cv2.line(leaf, (cx, cy), (cx + dx, cy + dy), (40, 90, 30), 2)
    if sick:
        for _ in range(25 * sick):
            cv2.circle(leaf, (cx + int(rng.normal(0, w * 0.1)), cy + int(rng.normal(0, h * 0.1))),
                       int(rng.integers(3, 9)), (110, 70, 30), -1)  # brown lesions
    m = leaf.any(axis=2)
    img[m] = leaf[m]
    # Leaf texture + sensor noise so the sharpness map has something to find
    noise = rng.normal(0, 2.5, img.shape)
    noise[m] += rng.normal(0, 9, (int(m.sum()), 3))
    return np.clip(img + noise, 0, 255).astype(np.uint8)


def make(cfg, mock_cfg):
    """Returns (camera, status). Falls back to the folder camera if the real one won't open."""
    try:
        if cfg.driver == "opencv":
            cam, status = OpenCVCamera(cfg), "ok"
        elif cfg.driver == "picamera":
            cam, status = PiCamera(cfg), "ok"
        else:
            cam, status = FolderCamera(cfg, mock_cfg.camera_folder), "mock"
    except Exception as e:
        log.error("camera (%s) failed to init: %s; using folder camera", cfg.driver, e)
        cam, status = FolderCamera(cfg, mock_cfg.camera_folder), "offline"
    return cam.start(), status
