"""VOC drift detector: is the air at this plant drifting away from healthy-plant air?

Diseased and stressed plants give off more VOCs (green-leaf volatiles, terpenes, ethylene,
rot products). Both sensors are metal-oxide: their resistance drops as VOCs rise. We don't
know absolute concentrations, but we do know what "normal" looks like, so this is a
statistical-process-control problem:

1. Features, per sensor, in log space (MOX resistance is roughly log-linear in concentration):
     x_mq2 = ln(Rs_mq2 / 1 kOhm)      from the MQ-2 AO voltage
     x_bme = ln(R_gas_bme680 / 1 Ohm)
   The BME680 gas resistance also falls with humidity (and, less so, temperature), which would
   read as fake VOCs, so each feature is humidity/temperature compensated with a slope
   fitted during calibration: x_comp = x - b_rh * (RH - RH_cal) - b_t * (T - T_cal).

2. Calibration: after warm-up, record calib_s seconds of clean air / healthy plants. Store the
   mean (mu) and standard deviation (sigma) of each compensated feature. sigma is floored at
   min_sigma so a suspiciously quiet calibration can't make every wiggle look like 10 sigma.

3. Detection, every sample:
     z = (mu - x) / sigma          (sign flipped: positive z = resistance down = more VOCs)
   - z_ewma: z smoothed (fast spikes from a passing smell are damped)
   - CUSUM:  S = max(0, S + z - k), the classic one-sided drift detector. It accumulates small,
     persistent shifts (0.5-1 sigma for minutes) that never trip a plain 3-sigma limit.
     Capped at cusum_max so it clears soon after the rover leaves a smelly plant.
   - slope:  sigma per minute from a least-squares fit over the last slope_window_s.

4. Verdict, fusing the two sensors (they have different selectivity, so agreement matters):
     diseased: both sensors past z_alarm, or both CUSUMs past cusum_h, held for hold_s
     watch:    either sensor past z_watch, or either CUSUM past cusum_h / 2
     healthy:  otherwise
   With only one sensor fitted, that sensor alone decides, and confidence is reported lower.

This is a screening signal, not a diagnosis: VOCs say "something changed here", the camera
says what.
"""
from __future__ import annotations

import collections
import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

SENSORS = ("mq2", "bme")


def _ok(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


def mean_std(xs: list[float]) -> tuple[float, float]:
    """Sample mean and (n-1) standard deviation."""
    n = len(xs)
    if n == 0:
        return math.nan, math.nan
    m = sum(xs) / n
    if n < 2:
        return m, 0.0
    return m, math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def ols_slope(xs: list[float], ys: list[float]) -> float | None:
    """Least-squares slope dy/dx, None if x doesn't vary."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def features(mq2_rs_kohm: float | None, gas_ohms: float | None) -> dict[str, float | None]:
    return {
        "mq2": math.log(mq2_rs_kohm) if _ok(mq2_rs_kohm) and mq2_rs_kohm > 0 else None,
        "bme": math.log(gas_ohms) if _ok(gas_ohms) and gas_ohms > 0 else None,
    }


@dataclass
class SensorCal:
    mu: float
    sigma: float  # floored
    sigma_raw: float  # as measured, before the floor
    b_rh: float = 0.0  # d(ln R)/d(%RH)
    b_t: float = 0.0  # d(ln R)/d(degC)
    n: int = 0


@dataclass
class DriftCal:
    sensors: dict[str, SensorCal] = field(default_factory=dict)
    rh_ref: float | None = None
    t_ref: float | None = None
    t: float | None = None  # when calibrated (unix)
    duration_s: float = 0.0

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: str | None) -> "DriftCal | None":
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                d = json.load(f)
            d["sensors"] = {k: SensorCal(**v) for k, v in d.get("sensors", {}).items()}
            cal = cls(**d)
            return cal if cal.sensors else None
        except Exception as e:
            log.warning("bad VOC drift calibration %s (%s); recalibrating", path, e)
            return None


def fit_calibration(rows: list[dict], cfg) -> DriftCal:
    """rows: [{t, mq2, bme, rh, temp}] (features already in log space). Fits the humidity /
    temperature compensation, then mean and sigma of the compensated features."""
    min_sigma = float(cfg.get("min_sigma", 0.01))
    max_b = float(cfg.get("max_comp_slope", 0.05))  # ln-units per %RH or degC; MOX datasheets ~0.01-0.03
    rhs = [r["rh"] for r in rows if _ok(r.get("rh"))]
    ts = [r["temp"] for r in rows if _ok(r.get("temp"))]
    rh_ref = mean_std(rhs)[0] if rhs else None
    t_ref = mean_std(ts)[0] if ts else None
    cal = DriftCal(rh_ref=rh_ref, t_ref=t_ref, t=time.time(),
                   duration_s=(rows[-1]["t"] - rows[0]["t"]) if len(rows) > 1 else 0.0)
    for name in SENSORS:
        pts = [r for r in rows if _ok(r.get(name))]
        if len(pts) < int(cfg.get("min_calib_samples", 10)):
            continue
        ys = [r[name] for r in pts]
        b_rh = b_t = 0.0
        # Only fit a slope if the calibration actually saw the climate move; otherwise the
        # "slope" is just noise and would inject error later.
        rh_pts = [r for r in pts if _ok(r.get("rh"))]
        if rh_ref is not None and len(rh_pts) >= 10 and \
                max(r["rh"] for r in rh_pts) - min(r["rh"] for r in rh_pts) >= float(cfg.get("min_rh_span", 3.0)):
            b_rh = ols_slope([r["rh"] for r in rh_pts], [r[name] for r in rh_pts]) or 0.0
        t_pts = [r for r in pts if _ok(r.get("temp"))]
        if t_ref is not None and len(t_pts) >= 10 and \
                max(r["temp"] for r in t_pts) - min(r["temp"] for r in t_pts) >= float(cfg.get("min_t_span", 2.0)):
            b_t = ols_slope([r["temp"] for r in t_pts], [r[name] for r in t_pts]) or 0.0
        b_rh = max(-max_b, min(max_b, b_rh))
        b_t = max(-max_b, min(max_b, b_t))
        comp = [_compensate(r[name], r.get("rh"), r.get("temp"), b_rh, b_t, rh_ref, t_ref) for r in pts]
        mu, sd = mean_std(comp)
        cal.sensors[name] = SensorCal(mu=mu, sigma=max(sd, min_sigma), sigma_raw=sd, b_rh=b_rh, b_t=b_t, n=len(comp))
    return cal


def _compensate(x, rh, temp, b_rh, b_t, rh_ref, t_ref) -> float:
    if b_rh and _ok(rh) and rh_ref is not None:
        x -= b_rh * (rh - rh_ref)
    if b_t and _ok(temp) and t_ref is not None:
        x -= b_t * (temp - t_ref)
    return x


class _Track:
    """Per-sensor running state after calibration."""

    def __init__(self, slope_n: int):
        self.z: float | None = None
        self.z_ewma: float | None = None
        self.cusum = 0.0
        self.peak_cusum = 0.0
        self.above_since: float | None = None  # z_ewma >= z_alarm since
        self.cusum_since: float | None = None  # cusum >= h since
        self.hist: collections.deque[tuple[float, float]] = collections.deque(maxlen=slope_n)


class DriftDetector:
    """Feed it one reading at a time with update(); read status() any time.

    States: no_data, waiting (sensors warming up) -> calibrating -> healthy | watch | diseased.
    """

    def __init__(self, cfg, path: str | None = None):
        self.cfg = cfg
        self.path = path if path is not None else cfg.get("calibration")
        self.calib_s = float(cfg.get("calib_s", 60))
        self.z_watch = float(cfg.get("z_watch", 2.0))
        self.z_alarm = float(cfg.get("z_alarm", 3.0))
        self.hold_s = float(cfg.get("hold_s", 5.0))
        self.k = float(cfg.get("cusum_k", 0.5))
        self.h = float(cfg.get("cusum_h", 8.0))
        # Clamp so one big smell can't latch the alarm for minutes: from the cap it takes
        # (cap - h) / k samples of normal air to clear. The rover moves between plants.
        self.cusum_max = float(cfg.get("cusum_max", 2 * self.h))
        self.alpha = float(cfg.get("ewma_alpha", 0.3))
        self.rate_hz = float(cfg.get("rate_hz", 1.0))
        self.slope_n = max(3, int(float(cfg.get("slope_window_s", 60)) * self.rate_hz))
        self.cal: DriftCal | None = DriftCal.load(self.path)
        self._calib_rows: list[dict] = []
        self._calib_start: float | None = None
        self._last_t = -math.inf
        self.tracks = {s: _Track(self.slope_n) for s in SENSORS}
        self.status_ = "calibrating" if self.cal is None else "healthy"
        self.since = time.time()
        self.last: dict = {}
        self.transitions = 0
        if self.cal:
            log.info("VOC drift: loaded calibration from %s (%s)", self.path,
                     ", ".join(f"{k} mu={v.mu:.3f} sigma={v.sigma:.4f}" for k, v in self.cal.sensors.items()))

    # ---------- control ----------
    def recalibrate(self) -> None:
        """Forget the old normal and learn a new one over the next calib_s seconds."""
        self.cal = None
        self._calib_rows, self._calib_start = [], None
        self.tracks = {s: _Track(self.slope_n) for s in SENSORS}
        self._set_status("calibrating")
        log.info("VOC drift: recalibrating for %.0f s, keep the sensors in clean air / at a healthy plant",
                 self.calib_s)

    def reset_cusum(self) -> None:
        for tr in self.tracks.values():
            tr.cusum = tr.peak_cusum = 0.0
            tr.above_since = tr.cusum_since = None

    # ---------- per sample ----------
    def update(self, t: float, mq2_rs_kohm: float | None, gas_ohms: float | None,
               rh: float | None = None, temp: float | None = None, ready: bool = True) -> dict | None:
        """ready=False while the sensors are still warming up. The calibration is never adapted
        while detecting, on purpose: a slow drift is exactly what we want to see.
        Returns the new status dict when a sample was taken, else None (rate-limited)."""
        if t - self._last_t < 1.0 / self.rate_hz - 1e-6:
            return None
        self._last_t = t
        f = features(mq2_rs_kohm, gas_ohms)
        if f["mq2"] is None and f["bme"] is None:
            self._set_status("no_data")
            return self.status()
        if not ready:
            self._set_status("waiting")
            return self.status()
        if self.cal is None:
            self._calibrate_step(t, f, rh, temp)
            return self.status()
        self._detect(t, f, rh, temp)
        return self.status()

    def _calibrate_step(self, t, f, rh, temp) -> None:
        if self.status_ != "calibrating":
            self._set_status("calibrating")
        if self._calib_start is None:
            self._calib_start = t
        self._calib_rows.append({"t": t, **f, "rh": rh, "temp": temp})
        if t - self._calib_start < self.calib_s:
            return
        cal = fit_calibration(self._calib_rows, self.cfg)
        self._calib_rows, self._calib_start = [], None
        if not cal.sensors:
            log.warning("VOC drift: calibration had too few valid samples; trying again")
            return
        self.cal = cal
        if self.path:
            try:
                cal.save(self.path)
            except OSError as e:
                log.warning("VOC drift: couldn't save calibration: %s", e)
        log.info("VOC drift calibrated: %s", ", ".join(
            f"{k} mu={v.mu:.3f} sigma={v.sigma:.4f} (raw {v.sigma_raw:.4f}) b_rh={v.b_rh:.4f}"
            for k, v in cal.sensors.items()))
        self._set_status("healthy")

    def _detect(self, t, f, rh, temp) -> None:
        cal = self.cal
        for name in SENSORS:
            sc, tr, x = cal.sensors.get(name), self.tracks[name], f[name]
            if sc is None or x is None:
                tr.z = None
                continue
            xc = _compensate(x, rh, temp, sc.b_rh, sc.b_t, cal.rh_ref, cal.t_ref)
            z = (sc.mu - xc) / sc.sigma
            tr.z = z
            tr.z_ewma = z if tr.z_ewma is None else tr.z_ewma + self.alpha * (z - tr.z_ewma)
            tr.cusum = min(self.cusum_max, max(0.0, tr.cusum + z - self.k))
            tr.peak_cusum = max(tr.peak_cusum, tr.cusum)
            tr.hist.append((t, z))
            tr.above_since = (tr.above_since or t) if tr.z_ewma >= self.z_alarm else None
            tr.cusum_since = (tr.cusum_since or t) if tr.cusum >= self.h else None
        live = [n for n in SENSORS if n in cal.sensors and self.tracks[n].z is not None]
        if not live:
            return
        trs = [self.tracks[n] for n in live]
        held = lambda since: since is not None and t - since >= self.hold_s
        alarm_z = all(held(tr.above_since) for tr in trs)
        alarm_cusum = all(held(tr.cusum_since) for tr in trs)
        watch = any(tr.z_ewma >= self.z_watch or tr.cusum >= self.h / 2 for tr in trs)
        self._set_status("diseased" if alarm_z or alarm_cusum else "watch" if watch else "healthy")

    def _set_status(self, s: str) -> None:
        if s != self.status_:
            log.info("VOC drift: %s -> %s", self.status_, s)
            self.status_ = s
            self.since = time.time()
            self.transitions += 1

    # ---------- output ----------
    def status(self) -> dict:
        cal = self.cal
        per = {}
        for name in SENSORS:
            tr, sc = self.tracks[name], cal.sensors.get(name) if cal else None
            hist = list(tr.hist)
            slope = None
            if len(hist) >= 3:
                s = ols_slope([h[0] for h in hist], [h[1] for h in hist])
                slope = None if s is None else s * 60.0  # sigma per minute
            per[name] = {
                "calibrated": sc is not None,
                "z": _r(tr.z), "z_ewma": _r(tr.z_ewma), "cusum": _r(tr.cusum), "peak_cusum": _r(tr.peak_cusum),
                "slope_per_min": _r(slope),
                "mu": _r(sc.mu, 4) if sc else None, "sigma": _r(sc.sigma, 5) if sc else None,
                "b_rh": _r(sc.b_rh, 5) if sc else None,
            }
        live = [per[n] for n in SENSORS if per[n]["calibrated"] and per[n]["z_ewma"] is not None]
        fused = sum(p["z_ewma"] for p in live) / len(live) if live else None
        cus = max((p["cusum"] for p in live), default=None)
        # 0-100 "VOC stress": 0 at the healthy mean, 100 at the alarm level (z or CUSUM).
        stress = None
        if live:
            zs = max(0.0, min(1.0, min(p["z_ewma"] for p in live) / self.z_alarm)) if len(live) > 1 \
                else max(0.0, min(1.0, live[0]["z_ewma"] / self.z_alarm))
            cs = max(0.0, min(1.0, min(p["cusum"] for p in live) / self.h))
            stress = round(100 * max(zs, cs), 1)
        progress = None
        if self.status_ == "calibrating":
            progress = 0.0 if self._calib_start is None or not self._calib_rows else \
                min(1.0, (self._calib_rows[-1]["t"] - self._calib_start) / self.calib_s)
        out = {
            "status": self.status_,
            "since": self.since,
            "z": _r(fused), "cusum": _r(cus), "stress": stress,
            "sensors": per,
            "n_sensors": len(live),
            "confidence": "high" if len(live) == 2 else "low" if live else None,
            "thresholds": {"z_watch": self.z_watch, "z_alarm": self.z_alarm, "cusum_h": self.h,
                           "cusum_k": self.k, "hold_s": self.hold_s},
            "calibration": None if not cal else {"t": cal.t, "duration_s": round(cal.duration_s, 1),
                                                 "rh_ref": _r(cal.rh_ref, 2), "t_ref": _r(cal.t_ref, 2)},
            "calib_progress": None if progress is None else round(progress, 3),
            "explain": self._explain(per, fused),
        }
        self.last = out
        return out

    def _explain(self, per, fused) -> str:
        s = self.status_
        if s == "no_data":
            return "No VOC sensors reporting"
        if s == "waiting":
            return "Sensors warming up"
        if s == "calibrating":
            return f"Learning healthy-air baseline ({self.calib_s:.0f} s): keep near a healthy plant"
        parts = []
        for name, label in (("mq2", "MQ-2"), ("bme", "BME680")):
            p = per[name]
            if p["z_ewma"] is not None:
                parts.append(f"{label} {p['z_ewma']:+.1f}σ")
        head = {"healthy": "VOCs within normal range", "watch": "VOCs drifting up",
                "diseased": "Sustained VOC rise: possible disease/stress"}[s]
        return head + (" · " + ", ".join(parts) if parts else "")


def _r(x, nd: int = 3):
    return None if x is None or not math.isfinite(x) else round(x, nd)
