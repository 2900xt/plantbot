"""Background sensor thread: ring buffer, smoothing, warm-up detection, baseline, differential, sniff()."""
from __future__ import annotations

import collections
import csv
import logging
import math
import os
import threading
import time

from plantbot.hal.env import EnvReading, mq2_rs_kohm
from plantbot.sensing.crosscal import CrossCal, differential, log_ratio

log = logging.getLogger(__name__)


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def _ewma(prev, x, a):
    if not _ok(x):
        return prev
    return x if not _ok(prev) else prev + a * (x - prev)


class Sampler:
    def __init__(self, env, cfg, mq2_cfg, csv_path: str | None = None):
        self.env, self.cfg, self.mq2_cfg = env, cfg, mq2_cfg
        self.lock = threading.Lock()
        n = int(cfg.sample_hz * 120)
        self.buf: collections.deque[EnvReading] = collections.deque(maxlen=n)
        self.smooth: dict[str, float | None] = {k: None for k in ("mq2_v", "gas_ohms", "temp_c", "rh", "pressure_hpa")}
        self.base_rs: float | None = None  # MQ-2 rs (kOhm) in clean air
        self.base_gas: float | None = None  # BME680 ohms in clean air
        self.track_baseline = True  # mission turns this off outside IDLE/CRUISE
        self._sniffing = 0
        self.t_start = time.time()
        self.warm = False
        self._stable_since: float | None = None
        self.csv_path = csv_path
        diff_cfg = cfg.get("differential")
        self.plant_sensor = diff_cfg.plant_sensor if diff_cfg else "mq2"
        self.crosscal = CrossCal.load(diff_cfg.calibration if diff_cfg else None)
        self._running = True
        threading.Thread(target=self._loop, name="sampler", daemon=True).start()

    # ---------- derived values ----------
    def rs(self, mq2_v) -> float | None:
        if not _ok(mq2_v):
            return None
        return mq2_rs_kohm(mq2_v, self.mq2_cfg.vc, self.mq2_cfg.rl_kohm)

    def ratios(self, mq2_v=None, gas=None) -> dict:
        """mq2 = rs_base / rs_now, gas = gas_base / gas_now. >1 means more VOCs.

        diff = exp(plant-side minus background log ratio, in BME680 units), so it reads like
        the other two: >1 means the plant side smells more than the background.
        """
        with self.lock:
            mq2_v = self.smooth["mq2_v"] if mq2_v is None else mq2_v
            gas = self.smooth["gas_ohms"] if gas is None else gas
            rs = self.rs(mq2_v)
            d = differential(log_ratio(self.base_rs, rs), log_ratio(self.base_gas, gas),
                             self.crosscal, self.plant_sensor)
            return {
                "mq2": self.base_rs / rs if rs and self.base_rs else None,
                "gas": self.base_gas / gas if _ok(gas) and self.base_gas else None,
                "diff": None if d is None else math.exp(d),
            }

    def voc_pair(self) -> tuple[float | None, float | None]:
        """(plant side, background) as ln(baseline / now) in BME680 units. Both rise with VOCs."""
        with self.lock:
            lr_mq2 = log_ratio(self.base_rs, self.rs(self.smooth["mq2_v"]))
            lr_bme = log_ratio(self.base_gas, self.smooth["gas_ohms"])
        mq2_on_bme = self.crosscal.mq2_to_bme(lr_mq2)
        return (mq2_on_bme, lr_bme) if self.plant_sensor == "mq2" else (lr_bme, mq2_on_bme)

    def warmup_remaining(self) -> float:
        if self.warm:
            return 0.0
        return max(0.0, self.cfg.warmup_s - (time.time() - self.t_start))

    def latest(self) -> EnvReading | None:
        with self.lock:
            return self.buf[-1] if self.buf else None

    def history(self, seconds: float) -> list[EnvReading]:
        cutoff = time.time() - seconds
        with self.lock:
            return [r for r in self.buf if r.t >= cutoff]

    # ---------- baseline ----------
    def recalibrate(self) -> None:
        """Snap the baseline to the current smoothed values (do this in clean air)."""
        with self.lock:
            self.base_rs = self.rs(self.smooth["mq2_v"])
            self.base_gas = self.smooth["gas_ohms"]
        log.info("baseline set: mq2 rs=%s kOhm, gas=%s ohm", self.base_rs, self.base_gas)

    def _update_baseline(self) -> None:
        s = self.smooth
        rs = self.rs(s["mq2_v"])
        if not self.warm:
            # During warm-up the baseline just follows the reading.
            self.base_rs, self.base_gas = rs or self.base_rs, s["gas_ohms"] or self.base_gas
            return
        if self.base_gas is None and _ok(s["gas_ohms"]):
            self.base_gas = s["gas_ohms"]
        if self.track_baseline and not self._sniffing:
            a = self.cfg.baseline_alpha
            self.base_rs = _ewma(self.base_rs, rs, a)
            self.base_gas = _ewma(self.base_gas, s["gas_ohms"], a)

    def _check_warm(self, now: float) -> None:
        el = now - self.t_start
        if el >= self.cfg.warmup_s:
            self._set_warm("timeout")
            return
        if el < self.cfg.warmup_min_s:
            return
        hist = [r for r in self.buf if r.t >= now - 5 and _ok(r.mq2_v)]
        if len(hist) < 5:
            return
        slope = abs(hist[-1].mq2_v - hist[0].mq2_v) / max(hist[-1].t - hist[0].t, 1e-3)
        if slope < self.cfg.warmup_stable_slope:
            self._stable_since = self._stable_since or now
            if now - self._stable_since >= self.cfg.warmup_stable_s:
                self._set_warm("stable")
        else:
            self._stable_since = None

    def _set_warm(self, why: str) -> None:
        self.warm = True
        log.info("sensors warmed up (%s) after %.0f s", why, time.time() - self.t_start)
        self.base_rs, self.base_gas = self.rs(self.smooth["mq2_v"]), self.smooth["gas_ohms"]

    # ---------- thread ----------
    def _loop(self) -> None:
        period = 1.0 / self.cfg.sample_hz
        last_csv = 0.0
        writer = f = None
        if self.csv_path:
            os.makedirs(os.path.dirname(self.csv_path), exist_ok=True)
            new = not os.path.exists(self.csv_path)
            f = open(self.csv_path, "a", newline="")
            writer = csv.writer(f)
            if new:
                writer.writerow(["t", "mq2_v", "gas_ohms", "temp_c", "rh", "pressure_hpa", "mq2_ratio", "gas_ratio", "sniffing"])
        while self._running:
            t0 = time.time()
            try:
                r = self.env.read()
                with self.lock:
                    self.buf.append(r)
                    for k in self.smooth:
                        self.smooth[k] = _ewma(self.smooth[k], getattr(r, k), self.cfg.smooth_alpha)
                    if not self.warm:
                        self._check_warm(t0)
                    self._update_baseline()
                if writer and t0 - last_csv >= 1.0 / self.cfg.csv_hz:
                    last_csv = t0
                    rt = self.ratios()
                    writer.writerow([f"{r.t:.2f}", r.mq2_v, r.gas_ohms, r.temp_c, r.rh, r.pressure_hpa,
                                     rt["mq2"], rt["gas"], int(bool(self._sniffing))])
                    f.flush()
            except Exception as e:
                log.warning("sensor read failed: %s", e)
            time.sleep(max(0.0, period - (time.time() - t0)))

    # ---------- sniff ----------
    def sniff(self, fan=None, settle_s: float | None = None, window_s: float | None = None,
              cancel: threading.Event | None = None) -> dict:
        """Blocking. Fan on, wait settle_s, average window_s. Baseline frozen meanwhile."""
        settle = self.cfg.sniff_settle_s if settle_s is None else settle_s
        window = self.cfg.sniff_window_s if window_s is None else window_s
        self._sniffing += 1
        try:
            if fan:
                fan.on()
            end_settle = time.time() + settle
            while time.time() < end_settle and not (cancel and cancel.is_set()):
                time.sleep(0.05)
            t_start = time.time()
            while time.time() < t_start + window and not (cancel and cancel.is_set()):
                time.sleep(0.05)
            rs = self.history(time.time() - t_start)
        finally:
            if fan:
                fan.off()
            self._sniffing -= 1

        def mean(key):
            vals = [getattr(r, key) for r in rs if _ok(getattr(r, key))]
            return sum(vals) / len(vals) if vals else None

        out = {k: mean(k) for k in ("mq2_v", "gas_ohms", "temp_c", "rh", "pressure_hpa")}
        rt = self.ratios(out["mq2_v"], out["gas_ohms"]) if rs else {"mq2": None, "gas": None, "diff": None}
        out.update(mq2_ratio=rt["mq2"], gas_ratio=rt["gas"], diff_ratio=rt["diff"],
                   diff_calibrated=self.crosscal.calibrated, plant_sensor=self.plant_sensor,
                   n=len(rs), window_s=window, baseline_ready=self.warm)
        return out

    def close(self) -> None:
        self._running = False
