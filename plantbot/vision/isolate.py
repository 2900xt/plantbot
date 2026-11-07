"""Crop the nearest plant out of a photo. No training needed.

plant-colored (green + yellow + brown, so lesions aren't cut out)
  AND sharp (near plant is in focus, background is blurred at our working distance)
  -> largest blob touching the image center -> bbox + padding.
Fallback: center crop, flagged as isolation="fallback".
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Isolation:
    crop: np.ndarray  # RGB
    mask_vis: np.ndarray  # uint8 gray: 80 = plant-colored, 160 = plant-colored+sharp, 255 = chosen blob
    bbox: tuple[int, int, int, int]  # x, y, w, h in full-res pixels
    method: str  # "blob" | "fallback"
    blob_frac: float


def color_mask(rgb: np.ndarray, cfg) -> np.ndarray:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    m = (h >= cfg.hue_min) & (h <= cfg.hue_max) & (s >= cfg.sat_min) & (v >= cfg.val_min)
    # Brown hues need more saturation: beige tables and wood floors are low-saturation brown.
    return m & ((h >= cfg.brown_hue_max) | (s >= cfg.brown_sat_min))


def sharp_mask(rgb: np.ndarray, cfg) -> np.ndarray:
    gray = cv2.GaussianBlur(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), (5, 5), 0)  # sensor noise isn't "sharp"
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    t = cfg.tile_px
    H, W = gray.shape
    th, tw = max(1, H // t), max(1, W // t)
    # Per-tile variance via mean of squares - square of means on a resized grid.
    m = cv2.resize(lap, (tw, th), interpolation=cv2.INTER_AREA)
    m2 = cv2.resize(lap * lap, (tw, th), interpolation=cv2.INTER_AREA)
    var = np.maximum(m2 - m * m, 0)
    thresh = max(cfg.sharp_abs_min, cfg.sharp_rel * float(np.percentile(var, 90)))
    tiles = (var > thresh).astype(np.uint8)
    return cv2.resize(tiles, (W, H), interpolation=cv2.INTER_NEAREST).astype(bool)


def _center_crop(rgb: np.ndarray, frac: float) -> tuple[int, int, int, int]:
    H, W = rgb.shape[:2]
    w, h = int(W * frac), int(H * frac)
    return (W - w) // 2, (H - h) // 2, w, h


def isolate(rgb: np.ndarray, cfg) -> Isolation:
    H, W = rgb.shape[:2]
    scale = min(1.0, cfg.work_width / W)
    small = cv2.resize(rgb, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else rgb
    sh, sw = small.shape[:2]

    col = color_mask(small, cfg)
    sharp = sharp_mask(small, cfg)
    both = (col & sharp).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    both = cv2.morphologyEx(both, cv2.MORPH_CLOSE, k)
    both = cv2.morphologyEx(both, cv2.MORPH_OPEN, k)

    # Central region the blob must touch
    cf = cfg.center_frac
    cx0, cy0, cx1, cy1 = int(sw * (1 - cf) / 2), int(sh * (1 - cf) / 2), int(sw * (1 + cf) / 2), int(sh * (1 + cf) / 2)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(both, connectivity=8)
    best, best_area = -1, 0
    for i in np.unique(labels[cy0:cy1, cx0:cx1]):
        if i > 0 and stats[i, cv2.CC_STAT_AREA] > best_area:
            best, best_area = int(i), int(stats[i, cv2.CC_STAT_AREA])

    vis = np.zeros((sh, sw), np.uint8)
    vis[col] = 80
    vis[col & sharp] = 160
    blob_frac = best_area / float(sw * sh)
    if best > 0 and blob_frac >= cfg.min_blob_frac:
        vis[labels == best] = 255
        x, y, w, h, _ = stats[best]
        px, py = int(w * cfg.pad_frac), int(h * cfg.pad_frac)
        x, y = max(0, x - px), max(0, y - py)
        w, h = min(sw - x, w + 2 * px), min(sh - y, h + 2 * py)
        bbox = (int(x / scale), int(y / scale), int(w / scale), int(h / scale))
        method = "blob"
    else:
        bbox = _center_crop(rgb, cfg.fallback_crop_frac)
        method = "fallback"
    cv2.rectangle(vis, (cx0, cy0), (cx1, cy1), 120, 1)
    x, y, w, h = bbox
    return Isolation(rgb[y:y + h, x:x + w].copy(), vis, bbox, method, round(blob_frac, 4))
