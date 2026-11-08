"""Uplink to the LeafLink server (the GPU box that runs the real leaf model and the field map).

Two jobs, both in one background thread so a slow or missing server never stalls the rover:
- each scan's raw photo goes to POST /api/captures (Idempotency-Key = scan id, so retries are safe)
- VOC drift detector snapshots (sensing/voc_drift.py: z-scores, CUSUM, verdict) go to
  POST /api/telemetry/drift in batches, for the dashboard's "VOC drift" card.
- VOC samples go to POST /api/telemetry/voc in batches. top = plant-side sensor, bottom = background
  sensor, both as 100 * ln(baseline / now) in BME680 units (cross-calibrated): roughly "% change",
  both rise with VOCs, and LeafLink's default rules (top +10, top minus bottom +5) read as
  "plant side up 10%, and 5% more than the background".

URL and token come from the environment (LEAFLINK_URL, LEAFLINK_TOKEN; on the Pi they live in
~/plantbot/.env, which systemd loads). Unset = disabled, and the rover runs exactly as before.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger(__name__)


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class LeafLink:
    def __init__(self, cfg, sampler):
        self.cfg = cfg
        self.sampler = sampler
        self.url = (os.getenv("LEAFLINK_URL") or cfg.get("url") or "").rstrip("/")
        self.token = os.getenv("LEAFLINK_TOKEN") or ""
        self.device = cfg.get("device", "rover-1")
        self.enabled = bool(cfg.get("enabled", True) and self.url and self.token)
        loc = (cfg.get("lat"), cfg.get("lon"))
        self.location = {"lat": loc[0], "lon": loc[1]} if None not in loc else None
        self.voc: collections.deque = collections.deque(maxlen=500)  # unsent samples; oldest dropped
        self.drift: collections.deque = collections.deque(maxlen=300)  # unsent VOC drift snapshots
        self.scans: collections.deque = collections.deque(maxlen=50)  # (scan_id, path, t)
        self.ok: bool | None = None
        self.last_error: str | None = None
        self.uploaded: dict[str, str] = {}  # scan id -> LeafLink capture id
        self._running = False
        self._wake = threading.Event()
        if not self.enabled:
            log.info("LeafLink uplink disabled (set LEAFLINK_URL and LEAFLINK_TOKEN to enable)")

    # ---------- public ----------
    def start(self) -> None:
        if not self.enabled:
            return
        self._running = True
        threading.Thread(target=self._loop, name="leaflink", daemon=True).start()
        log.info("LeafLink uplink -> %s as %s", self.url, self.device)

    def close(self) -> None:
        self._running = False
        self._wake.set()

    def submit_scan(self, scan_id: str, jpg_path: str, t: float) -> None:
        if self.enabled:
            self.scans.append((scan_id, jpg_path, t))
            self._wake.set()

    def status(self) -> dict:
        return {"enabled": self.enabled, "url": self.url or None, "device": self.device, "ok": self.ok,
                "error": self.last_error, "pending_voc": len(self.voc), "pending_scans": len(self.scans)}

    # ---------- thread ----------
    def _loop(self) -> None:
        period = 1.0 / float(self.cfg.get("voc_hz", 1))
        flush_s = float(self.cfg.get("flush_s", 3))
        last_sample = last_flush = 0.0
        while self._running:
            now = time.time()
            if now - last_sample >= period:
                last_sample = now
                self._sample(now)
            if self.scans:
                self._send_scan(*self.scans[0])
            if (self.voc or self.drift) and now - last_flush >= flush_s:
                last_flush = now
                if self.voc:
                    self._send_voc()
                if self.drift:
                    self._send_drift()
            self._wake.wait(min(period, 0.5))
            self._wake.clear()

    def _sample(self, t: float) -> None:
        self._sample_drift(t)
        if not self.sampler.warm:
            return  # LeafLink fixes its reference from the first sample: don't feed it warm-up drift
        top, bottom = self.sampler.voc_pair()
        if top is None or bottom is None:
            return
        s = {"sample_id": f"{self.device}-{int(t * 1000)}", "measured_at": _iso(t),
             "top": round(100 * top, 3), "bottom": round(100 * bottom, 3), "units": "pct_ln_ratio_bme"}
        if self.location:
            s["location"] = self.location
        self.voc.append(s)

    def _sample_drift(self, t: float) -> None:
        d = getattr(self.sampler, "drift", None)
        st = d.last if d else None
        if not st or st["status"] == "no_data":
            return
        keep = ("z", "z_ewma", "cusum", "slope_per_min")
        snap = {k: st[k] for k in ("status", "z", "cusum", "stress", "confidence", "calib_progress", "explain")}
        snap.update(measured_at=_iso(t), device=self.device, thresholds=st["thresholds"],
                    sensors={n: {k: v[k] for k in keep} for n, v in st["sensors"].items() if v["calibrated"]})
        if self.location:
            snap["location"] = self.location
        self.drift.append(snap)

    def _send_drift(self) -> None:
        batch = list(self.drift)
        try:
            self._post_json("/api/telemetry/drift", batch)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                for _ in batch:
                    self.drift.popleft()
            self._fail(f"drift upload HTTP {e.code}: {e.read()[:200].decode(errors='replace')}")
            return
        except Exception as e:
            self._fail(f"drift upload failed: {e}")
            return
        for _ in batch:
            self.drift.popleft()
        self._good()

    def _send_voc(self) -> None:
        batch = list(self.voc)
        try:
            self._post_json("/api/telemetry/voc", [dict(s, device=self.device) for s in batch])
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                # Rejected (e.g. a timestamp went backwards): drop it rather than retry forever.
                for _ in batch:
                    self.voc.popleft()
            self._fail(f"VOC upload HTTP {e.code}: {e.read()[:200].decode(errors='replace')}")
            return
        except Exception as e:
            self._fail(f"VOC upload failed: {e}")
            return
        for _ in batch:
            self.voc.popleft()
        self._good()

    def _send_scan(self, scan_id: str, path: str, t: float) -> None:
        try:
            with open(path, "rb") as f:
                image = f.read()
        except OSError as e:
            log.error("LeafLink: can't read %s: %s", path, e)
            self.scans.popleft()
            return
        meta = {"captured_at": _iso(t)}
        if self.location:
            meta["location"] = self.location
        boundary = uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="device"\r\n\r\n{self.device}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n{json.dumps(meta)}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{scan_id}.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode() + image + f"\r\n--{boundary}--\r\n".encode()
        try:
            row = self._request("/api/captures", body, {
                "Content-Type": f"multipart/form-data; boundary={boundary}", "Idempotency-Key": scan_id})
        except urllib.error.HTTPError as e:
            msg = e.read()[:200].decode(errors="replace")
            if e.code < 500 and e.code != 429:
                log.error("LeafLink rejected scan %s (HTTP %d): %s", scan_id, e.code, msg)
                self.scans.popleft()
            self._fail(f"scan upload HTTP {e.code}: {msg}")
            time.sleep(2)
            return
        except Exception as e:
            self._fail(f"scan upload failed: {e}")
            time.sleep(2)
            return
        self.scans.popleft()
        self.uploaded[scan_id] = str(row.get("id", "")) if isinstance(row, dict) else ""
        log.info("LeafLink: scan %s uploaded as capture %s", scan_id, self.uploaded[scan_id])
        self._good()

    # ---------- http ----------
    def _post_json(self, path: str, obj) -> dict:
        return self._request(path, json.dumps(obj).encode(), {"Content-Type": "application/json"})

    def _request(self, path: str, body: bytes, headers: dict) -> dict:
        req = urllib.request.Request(self.url + path, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {self.token}", **headers})
        with urllib.request.urlopen(req, timeout=float(self.cfg.get("timeout_s", 10))) as r:
            data = r.read()
        return json.loads(data) if data else {}

    def _good(self) -> None:
        if self.ok is not True:
            log.info("LeafLink: connected")
        self.ok, self.last_error = True, None

    def _fail(self, msg: str) -> None:
        if msg != self.last_error:
            log.warning("LeafLink: %s", msg)
        self.ok, self.last_error = False, msg
