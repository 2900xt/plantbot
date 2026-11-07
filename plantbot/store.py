"""Scan records: SQLite index + scans/<id>/{raw.jpg, mask.png, crop.jpg, scan.json}."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time

import cv2

COLS = ["id", "t", "mode", "label", "p_healthy", "mq2_ratio", "gas_ratio", "temp_c", "rh",
        "health", "status", "isolation", "infer_ms", "backend", "diff_ratio"]


class Store:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(os.path.join(root, "scans.db"), check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS scans (id TEXT PRIMARY KEY, t REAL, mode TEXT, label TEXT, "
            "p_healthy REAL, mq2_ratio REAL, gas_ratio REAL, temp_c REAL, rh REAL, health REAL, "
            "status TEXT, isolation TEXT, infer_ms REAL, backend TEXT, diff_ratio REAL)")
        have = {r[1] for r in self.db.execute("PRAGMA table_info(scans)")}
        if "diff_ratio" not in have:  # scans.db from before the differential existed
            self.db.execute("ALTER TABLE scans ADD COLUMN diff_ratio REAL")
        self.db.commit()

    @staticmethod
    def new_id() -> str:
        return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)

    def save(self, rec: dict, raw, mask, crop) -> dict:
        """rec must have every COLS key. Writes images + json + db row; returns rec with image URLs."""
        d = os.path.join(self.root, rec["id"])
        os.makedirs(d, exist_ok=True)
        cv2.imwrite(os.path.join(d, "raw.jpg"), cv2.cvtColor(raw, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])
        cv2.imwrite(os.path.join(d, "mask.png"), mask)
        cv2.imwrite(os.path.join(d, "crop.jpg"), cv2.cvtColor(crop, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 90])
        rec = {**rec, "images": self.image_urls(rec["id"])}
        with open(os.path.join(d, "scan.json"), "w") as f:
            json.dump(rec, f, indent=2, default=str)
        with self.lock:
            self.db.execute(f"INSERT OR REPLACE INTO scans ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                            [rec.get(c) for c in COLS])
            self.db.commit()
        return rec

    @staticmethod
    def image_urls(scan_id: str) -> dict:
        return {k: f"/scans/{scan_id}/{k}.{ext}" for k, ext in (("raw", "jpg"), ("mask", "png"), ("crop", "jpg"))}

    def list(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute(f"SELECT {','.join(COLS)} FROM scans ORDER BY t DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(zip(COLS, r)), "images": self.image_urls(r[0])} for r in rows]

    def get(self, scan_id: str) -> dict | None:
        p = os.path.join(self.root, os.path.basename(scan_id), "scan.json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)
