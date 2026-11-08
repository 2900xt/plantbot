"""Stream live rover telemetry to Zelos (zeloscloud.io) for the hardware-observability view.

Zelos = desktop app + local agent; the open-source Python SDK (`pip install zelos-sdk`,
import `zelos_sdk`, abi3 wheels incl. manylinux aarch64) publishes over gRPC to the agent,
default http://localhost:2300, override with ZELOS_AGENT_URL or config `zelos.url`
(e.g. grpc://<laptop-ip>:2300 when the Zelos app runs on the laptop and the rover publishes).

    zelos_sdk.init("plantbot", url=...)        # non-blocking; agent may come up later
    src = zelos_sdk.TraceSource("env")
    ev = src.add_event("voc", [TraceEventFieldMetadata("mq2_v", DataType.Float64, "V"), ...])
    ev.log(mq2_v=0.81, ...)

Without the SDK, or with `zelos.enabled: false`, every method is a no-op, so this is safe to
wire in unconditionally. Publishing runs on its own thread and never touches the event loop.

Sources / events (signal paths in Zelos are source/event/field):
  env/voc        mq2_v (raw), mq2_v_smooth, gas_ohms (raw), gas_ohms_smooth, mq2_ratio, gas_ratio, diff_ratio
  env/climate    temp_c, rh, pressure_hpa
  drive/motors   left, right (duty -1..1), range_cm, watchdog_trips
  mission/state  state, mode, auto_scans, warm
  tactile/frame  total, peak, area_px, pixels_on, cop_row, cop_col, contact, rate_hz  (tactile_hz)
  tactile/bump   peak_force, duration_ms, cop_row, cop_col                           (per contact)
  scan/result    health, p_healthy, voc_stress, turgor_index, peak_force, pressure_at_ref, status, label
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger(__name__)

NAN = float("nan")


def _f(x) -> float:
    try:
        return NAN if x is None else float(x)
    except (TypeError, ValueError):
        return NAN


class ZelosPublisher:
    def __init__(self, cfg=None, mission=None, tactile=None, guard=None):
        get = (lambda k, d: cfg.get(k, d)) if cfg is not None else (lambda k, d: d)
        self.enabled = bool(get("enabled", True))
        self.rate_hz = float(get("rate_hz", 10))
        self.tactile_hz = float(get("tactile_hz", 50))
        self.url = get("url", None) or os.environ.get("ZELOS_AGENT_URL")
        self.mission, self.tactile, self.guard = mission, tactile, guard
        self.z = None
        self.ev: dict = {}
        self.sent = 0
        self.error: str | None = None
        self._running = False
        self._last_tac = 0.0
        if not self.enabled:
            return
        try:
            import zelos_sdk as z  # optional dependency
        except ImportError:
            self.enabled = False
            self.error = "zelos-sdk not installed"
            log.info("zelos: SDK not installed, publisher disabled")
            return
        try:
            z.init(get("name", "plantbot"), url=self.url)
            self.z = z
            self._schema()
        except Exception as e:
            self.enabled, self.error = False, str(e)
            log.warning("zelos: init failed (%s), publisher disabled", e)

    # ---------------------------------------------------------------- schema
    def _schema(self) -> None:
        z = self.z
        F, S, B, I = z.DataType.Float64, z.DataType.String, z.DataType.Boolean, z.DataType.Int64

        def ev(src, name, fields):
            return src.add_event(name, [z.TraceEventFieldMetadata(n, t, *([u] if u else [])) for n, t, u in fields])

        env, drive, mission = z.TraceSource("env"), z.TraceSource("drive"), z.TraceSource("mission")
        tac, scan = z.TraceSource("tactile"), z.TraceSource("scan")
        self.ev = {
            "voc": ev(env, "voc", [("mq2_v", F, "V"), ("mq2_v_smooth", F, "V"), ("gas_ohms", F, "ohm"),
                                   ("gas_ohms_smooth", F, "ohm"), ("mq2_ratio", F, None), ("gas_ratio", F, None),
                                   ("diff_ratio", F, None)]),
            "climate": ev(env, "climate", [("temp_c", F, "°C"), ("rh", F, "%"), ("pressure_hpa", F, "hPa")]),
            "motors": ev(drive, "motors", [("left", F, None), ("right", F, None), ("range_cm", F, "cm"),
                                           ("watchdog_trips", I, None)]),
            "state": ev(mission, "state", [("state", S, None), ("mode", S, None), ("auto_scans", I, None),
                                           ("warm", B, None)]),
            "frame": ev(tac, "frame", [("total", F, "a.u."), ("peak", F, "a.u."), ("area_px", F, "px"),
                                       ("pixels_on", I, "px"), ("cop_row", F, "px"), ("cop_col", F, "px"),
                                       ("contact", B, None), ("rate_hz", F, "Hz")]),
            "bump": ev(tac, "bump", [("peak_force", F, "a.u."), ("duration_ms", F, "ms"), ("cop_row", F, "px"),
                                     ("cop_col", F, "px")]),
            "scan": ev(scan, "result", [("health", F, None), ("p_healthy", F, None), ("voc_stress", F, None),
                                        ("turgor_index", F, None), ("peak_force", F, "a.u."),
                                        ("pressure_at_ref", F, "a.u./px"), ("status", S, None), ("label", S, None)]),
        }

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if not self.enabled or self._running:
            return
        self._running = True
        if self.tactile is not None and hasattr(self.tactile, "subscribe"):
            self.tactile.subscribe(self._on_tactile)
        threading.Thread(target=self._loop, name="zelos-pub", daemon=True).start()
        log.info("zelos: publishing to %s", self.url or "http://localhost:2300")

    def close(self) -> None:
        self._running = False

    def status(self) -> dict:
        return {"enabled": self.enabled, "sent": self.sent, "error": self.error, "url": self.url}

    def _log(self, key: str, **fields) -> None:
        if not self.enabled or key not in self.ev:
            return
        try:
            self.ev[key].log(**fields)
            self.sent += 1
        except Exception as e:  # never let telemetry take the rover down
            if self.error != str(e):
                log.warning("zelos: log %s failed: %s", key, e)
            self.error = str(e)

    # ---------------------------------------------------------------- publishers
    def _loop(self) -> None:
        period = 1.0 / self.rate_hz
        while self._running:
            t0 = time.monotonic()
            try:
                self.publish_snapshot()
            except Exception:
                log.exception("zelos: snapshot failed")
            time.sleep(max(0.0, period - (time.monotonic() - t0)))

    def publish_snapshot(self) -> None:
        m = self.mission
        if m is None:
            return
        r = m.r
        s = r.sampler
        raw = s.latest()
        with s.lock:
            sm = dict(s.smooth)
        rt = s.ratios()
        self._log("voc", mq2_v=_f(getattr(raw, "mq2_v", None)), mq2_v_smooth=_f(sm.get("mq2_v")),
                  gas_ohms=_f(getattr(raw, "gas_ohms", None)), gas_ohms_smooth=_f(sm.get("gas_ohms")),
                  mq2_ratio=_f(rt.get("mq2")), gas_ratio=_f(rt.get("gas")), diff_ratio=_f(rt.get("diff")))
        self._log("climate", temp_c=_f(sm.get("temp_c")), rh=_f(sm.get("rh")), pressure_hpa=_f(sm.get("pressure_hpa")))
        self._log("motors", left=_f(r.motors.left), right=_f(r.motors.right), range_cm=_f(m.range_cm),
                  watchdog_trips=int(getattr(r.motors, "watchdog_trips", 0)))
        self._log("state", state=str(m.state), mode=str(m.mode), auto_scans=int(m.auto_scans), warm=bool(s.warm))

    def _on_tactile(self, f) -> None:
        """Reader thread, ~100 Hz: decimate to tactile_hz."""
        if f.t - self._last_tac < 1.0 / self.tactile_hz:
            return
        self._last_tac = f.t
        cop = f.cop or (NAN, NAN)
        contact = bool(self.guard.contact) if self.guard is not None else f.total > 0
        rate = self.tactile.rate_hz() if hasattr(self.tactile, "rate_hz") else NAN
        self._log("frame", total=f.total, peak=f.peak, area_px=f.area, pixels_on=int(f.n_on), cop_row=_f(cop[0]),
                  cop_col=_f(cop[1]), contact=contact, rate_hz=_f(rate))

    def publish_bump(self, e: dict) -> None:
        cop = e.get("cop") or (None, None)
        self._log("bump", peak_force=_f(e.get("peak_force")), duration_ms=_f(e.get("duration_ms")),
                  cop_row=_f(cop[0]), cop_col=_f(cop[1]))

    def publish_scan(self, rec: dict) -> None:
        t = rec.get("turgor") or {}
        self._log("scan", health=_f(rec.get("health")), p_healthy=_f(rec.get("p_healthy")),
                  voc_stress=_f(rec.get("voc_stress")), turgor_index=_f(rec.get("turgor_index")),
                  peak_force=_f(t.get("peak_force")), pressure_at_ref=_f(t.get("pressure_at_ref")),
                  status=str(rec.get("status")), label=str(rec.get("label")))


def make(cfg, mission=None, tactile=None, guard=None) -> ZelosPublisher:
    return ZelosPublisher(cfg, mission, tactile, guard)


__all__ = ["ZelosPublisher", "make"]
