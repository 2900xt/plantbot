"""Leaf turgor / wilt probe on the SensX25.

Idea: press a soft pad onto a leaf and watch force grow against contact area. A turgid
(well-watered) leaf is stiff: force climbs fast while the contact patch stays small, so the
mean contact pressure is high. A wilted leaf is limp: it drapes over the pad, the same force
spreads over more pixels, and the pressure stays low. One probe -> turgor index 0..1.

Units: SensX25 pressure mode is arbitrary units (0..534 per pixel), so "force" here is the
pixel sum in a.u. and area is in pixels. The index is only meaningful after calibrating the
two reference values on a known-watered and a known-wilted leaf of the same species
(see calibrate()). The defaults come from the mock model and are (unverified) for real leaves.

Pure signal processing lives in analyze(); probe() runs the arm and records frames.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field

import numpy as np

log = logging.getLogger(__name__)

DEFAULTS = {
    "contact_on": 20.0,         # a.u. total: contact starts above this (unverified)
    "contact_off": 10.0,        # ...and ends below this (hysteresis)
    "debounce_frames": 3,       # consecutive frames over contact_on (~30 ms at 100 Hz)
    "ref_force": 250.0,         # a.u. total at which the pressure metric is read (unverified)
    "target_force": 400.0,      # stop pressing at this total (unverified; gentle on the leaf)
    "max_peak": 500.0,          # abort if any pixel reaches this (534 = saturation)
    "ramp_s": 2.5,              # arm 0 -> 1 over this long (or until target_force)
    "hold_s": 0.5,
    "settle_s": 0.3,
    "zero_s": 0.5,              # baseline frames with the arm retracted
    "turgid_ref": 55.0,         # mean contact pressure (a.u./px) of a well-watered leaf (mock-derived, unverified)
    "wilted_ref": 12.0,         # ...and of a wilted one (mock-derived, unverified)
    "edge_margin": 0.6,         # CoP within this many px of the border = leaf only half on the pad
    "push_speed": 0.18,         # rover-push mode: forward duty while pressing
    "push_timeout_s": 3.0,
    "backoff_s": 0.4,
    "calibration": "calibration/turgor.json",
    "w_turgor": 0.25,           # weight of turgor in the fused health score
}


def _cfg(cfg) -> dict:
    out = dict(DEFAULTS)
    if cfg is not None:
        src = cfg.to_dict() if hasattr(cfg, "to_dict") else dict(cfg)
        out.update({k: v for k, v in src.items() if k in DEFAULTS and v is not None})
    return out


# ---------------------------------------------------------------- contact detection
class ContactDetector:
    """Hysteresis + debounce on the frame total. update(total) -> True while in contact."""

    def __init__(self, on: float, off: float, debounce: int = 3):
        self.on, self.off, self.debounce = on, off, debounce
        self.state = False
        self._n = 0

    def update(self, total: float) -> bool:
        if not self.state:
            self._n = self._n + 1 if total >= self.on else 0
            if self._n >= self.debounce:
                self.state = True
        elif total < self.off:
            self.state, self._n = False, 0
        return self.state


# ---------------------------------------------------------------- analysis
@dataclass
class TurgorResult:
    ok: bool
    turgor_index: float | None      # 0 = wilted .. 1 = turgid
    label: str                      # turgid | mild stress | wilted | no contact | ...
    peak_force: float = 0.0         # a.u. (pixel sum)
    peak_pixel: float = 0.0
    contact_area_px: float = 0.0    # effective area at peak (participation ratio, pixels)
    pixels_on: int = 0
    pressure_at_ref: float | None = None   # a.u. per pixel at ref_force: the turgor metric
    stiffness_area: float | None = None    # d(force)/d(area), a.u. per pixel, loading phase
    stiffness_pos: float | None = None     # d(force)/d(arm position), a.u. per unit travel
    force_rate: float | None = None        # d(force)/dt, a.u./s, loading phase
    cop: list | None = None
    contact_ms: float = 0.0
    n_frames: int = 0
    flags: list = field(default_factory=list)
    curve: list = field(default_factory=list)   # [[t, force, area, pos], ...] downsampled for plots

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = None if math.isnan(v) else round(v, 4)
        return d


def _slope(x: np.ndarray, y: np.ndarray) -> float | None:
    if len(x) < 4 or np.ptp(x) <= 1e-9:
        return None
    return float(np.polyfit(x, y, 1)[0])


def _interp_pos(t: np.ndarray, positions) -> np.ndarray | None:
    if not positions:
        return None
    pt = np.array([p[0] for p in positions], float)
    pv = np.array([p[1] for p in positions], float)
    return np.interp(t, pt, pv)


def turgor_index(pressure: float | None, cfg) -> float | None:
    c = _cfg(cfg)
    if pressure is None:
        return None
    lo, hi = c["wilted_ref"], c["turgid_ref"]
    if hi <= lo:
        return None
    return float(min(1.0, max(0.0, (pressure - lo) / (hi - lo))))


def label_for(idx: float | None) -> str:
    if idx is None:
        return "unknown"
    return "turgid" if idx >= 0.66 else "mild stress" if idx >= 0.33 else "wilted"


def analyze(frames, positions=None, cfg=None) -> TurgorResult:
    """frames: TactileFrames (or anything with .t, .grid) covering one press.
    positions: optional [(t, arm_pos 0..1)] samples for the displacement stiffness."""
    c = _cfg(cfg)
    if not frames:
        return TurgorResult(False, None, "no data", flags=["no frames"])
    t = np.array([f.t for f in frames], float)
    grids = np.stack([np.asarray(f.grid, float) for f in frames])
    force = grids.reshape(len(frames), -1).sum(1)
    sq = (grids.reshape(len(frames), -1) ** 2).sum(1)
    area = np.divide(force ** 2, sq, out=np.zeros_like(force), where=sq > 0)

    det = ContactDetector(c["contact_on"], c["contact_off"], int(c["debounce_frames"]))
    in_contact = np.array([det.update(x) for x in force])
    if not in_contact.any():
        return TurgorResult(False, None, "no contact", peak_force=float(force.max()), n_frames=len(frames),
                            flags=["no contact: leaf missed or pad too far"])

    first = int(np.argmax(in_contact)) - int(c["debounce_frames"]) + 1
    first = max(first, 0)
    i_pk = int(np.argmax(force))
    if i_pk <= first:
        i_pk = first
    load = slice(first, i_pk + 1)
    lt, lf, la = t[load], force[load], area[load]

    flags = []
    peak_grid = grids[i_pk]
    peak_pixel = float(peak_grid.max())
    if peak_pixel >= 0.95 * 534:
        flags.append("saturated: area underestimated, soften the pad or press less")
    rows, cols = np.indices(peak_grid.shape)
    cop = None
    if force[i_pk] > 0:
        cop = [float((peak_grid * rows).sum() / force[i_pk]), float((peak_grid * cols).sum() / force[i_pk])]
        m = c["edge_margin"]
        if min(cop[0], cop[1], 4 - cop[0], 4 - cop[1]) < m:
            flags.append("edge contact: leaf only partly on the pad")

    # pressure metric at a common force, so a harder or softer press doesn't move the index
    ref = c["ref_force"]
    if lf.max() >= ref and len(lf) >= 2:
        j = int(np.argmax(lf >= ref))
        if j == 0:
            a_ref = la[0]
        else:  # linear interpolation between the two frames straddling ref
            w = (ref - lf[j - 1]) / max(lf[j] - lf[j - 1], 1e-9)
            a_ref = la[j - 1] + w * (la[j] - la[j - 1])
        pressure = ref / a_ref if a_ref > 0 else None
    else:
        pressure = float(force[i_pk] / area[i_pk]) if area[i_pk] > 0 else None
        flags.append(f"peak force {force[i_pk]:.0f} < ref_force {ref:.0f}: pressure read at peak")

    stiff_area = _slope(la, lf)
    force_rate = _slope(lt, lf)
    pos = _interp_pos(lt, positions)
    stiff_pos = _slope(pos, lf) if pos is not None else None

    contact_ms = float((t[in_contact][-1] - t[in_contact][0]) * 1000) if in_contact.sum() > 1 else 0.0
    if len(lt) < 5:
        flags.append("few loading frames: press slower")

    idx = turgor_index(pressure, c)
    allpos = _interp_pos(t, positions)
    step = max(1, len(t) // 120)
    curve = [[round(float(t[i] - t[0]), 3), round(float(force[i]), 1), round(float(area[i]), 2),
              None if allpos is None else round(float(allpos[i]), 3)] for i in range(0, len(t), step)]
    return TurgorResult(
        ok=idx is not None, turgor_index=idx, label=label_for(idx),
        peak_force=float(force[i_pk]), peak_pixel=peak_pixel, contact_area_px=float(area[i_pk]),
        pixels_on=int((peak_grid > 0).sum()), pressure_at_ref=pressure, stiffness_area=stiff_area,
        stiffness_pos=stiff_pos, force_rate=force_rate, cop=cop, contact_ms=contact_ms,
        n_frames=len(frames), flags=flags, curve=curve)


# ---------------------------------------------------------------- calibration
def load_calibration(cfg) -> dict:
    """Overlay calibration/turgor.json (written by calibrate()) on the config refs."""
    c = _cfg(cfg)
    path = c["calibration"]
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                c.update({k: v for k, v in json.load(f).items() if k in ("turgid_ref", "wilted_ref")})
        except Exception:
            log.exception("bad turgor calibration file %s", path)
    return c


def calibrate(kind: str, result: TurgorResult, cfg) -> dict:
    """kind = "turgid" | "wilted": store this probe's pressure as that reference."""
    if kind not in ("turgid", "wilted") or result.pressure_at_ref is None:
        raise ValueError("need kind turgid|wilted and a probe with contact")
    c = load_calibration(cfg)
    c[f"{kind}_ref"] = round(float(result.pressure_at_ref), 3)
    path = c["calibration"]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"turgid_ref": c["turgid_ref"], "wilted_ref": c["wilted_ref"], "t": time.time()}, f, indent=1)
    log.info("turgor calibration: %s_ref = %.2f", kind, c[f"{kind}_ref"])
    return c


# ---------------------------------------------------------------- the press itself
def probe(tactile, arm, cfg=None, motors=None, guard=None, cancel: threading.Event | None = None) -> TurgorResult:
    """Blocking (run it in a thread). Press, record, retract, analyze.

    arm.kind == "rover": no arm; creep the rover forward at push_speed until target_force,
    then back off. Otherwise ramp the arm 0 -> 1 over ramp_s, stopping early at target_force.
    guard: a ContactGuard to suspend, so the bumper doesn't treat the leaf as a crash."""
    c = load_calibration(cfg)
    cancel = cancel or threading.Event()
    positions: list[tuple[float, float]] = []
    suspended = guard.suspended() if guard is not None else _nullctx()
    with suspended:
        arm.retract()
        time.sleep(c["settle_s"])
        tactile.calibrate_zero(c["zero_s"])
        t0 = time.time()
        try:
            if getattr(arm, "kind", "") == "rover":
                if motors is None:
                    raise RuntimeError("rover-push probe needs motors")
                t_end = time.monotonic() + c["push_timeout_s"]
                while time.monotonic() < t_end and not cancel.is_set():
                    f = tactile.latest()
                    if f and (f.total >= c["target_force"] or f.peak >= c["max_peak"]):
                        break
                    motors.drive(c["push_speed"], c["push_speed"])
                    time.sleep(0.02)
                motors.stop()
                time.sleep(c["hold_s"])
                motors.drive(-c["push_speed"], -c["push_speed"])
                time.sleep(c["backoff_s"])
                motors.stop()
            else:
                steps = max(10, int(c["ramp_s"] * 50))
                for i in range(steps + 1):
                    if cancel.is_set():
                        break
                    u = i / steps
                    arm.move_to(u)
                    positions.append((time.time(), u))
                    time.sleep(c["ramp_s"] / steps)
                    f = tactile.latest()
                    if f and (f.total >= c["target_force"] or f.peak >= c["max_peak"]):
                        break
                time.sleep(c["hold_s"])
                positions.append((time.time(), arm.position()))
        finally:
            arm.retract()
            if motors is not None and getattr(arm, "kind", "") == "rover":
                motors.stop()
        time.sleep(c["settle_s"])
        arm.relax()
    frames = [f for f in tactile.history(time.time() - t0 + 0.1) if f.t >= t0]
    res = analyze(frames, positions or None, c)
    if cancel.is_set():
        res.flags.append("cancelled")
    log.info("turgor: %s idx=%s P@ref=%s peak=%.0f area=%.1f %s", res.label, res.turgor_index,
             res.pressure_at_ref, res.peak_force, res.contact_area_px, res.flags)
    return res


class _nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------- fusion
def fuse_with_turgor(fused: dict, result: TurgorResult | dict | None, cfg=None, fusion_cfg=None) -> dict:
    """Blend the turgor index into fusion.fuse()'s output without touching fusion.py.
    health' = (1 - w_turgor) * health + w_turgor * 100 * turgor. No contact -> unchanged, says so.
    cfg = config `tactile.turgor`; fusion_cfg = config `fusion` (for the status thresholds)."""
    c = _cfg(cfg)
    out = dict(fused)
    r = result.to_dict() if isinstance(result, TurgorResult) else (result or {})
    idx = r.get("turgor_index")
    if idx is None:
        out["turgor_index"] = None
        out["explain"] = fused.get("explain", "") + " · Touch: no reading"
        return out
    w = c["w_turgor"]
    health = (1 - w) * fused["health"] + w * 100 * idx
    out["health"] = round(health, 1)
    out["turgor_index"] = round(idx, 3)
    out["explain"] = fused.get("explain", "") + f" · Touch: {r.get('label') or label_for(idx)} ({idx * 100:.0f}% turgor)"
    hi = getattr(fusion_cfg, "healthy_min", 70) if fusion_cfg is not None else 70
    lo = getattr(fusion_cfg, "watch_min", 40) if fusion_cfg is not None else 40
    out["status"] = "healthy" if health >= hi else "watch" if health >= lo else "unhealthy"
    return out
