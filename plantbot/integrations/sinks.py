"""Telemetry sinks: a tiny interface plus a fan-out that keeps every sink off the hot path.

Each sink gets its own bounded queue and daemon thread, so a slow or dead cloud service can
never stall the 10 Hz sampler, the motors or another sink. When a queue is full the newest
item is dropped and counted (we never block the caller). Sinks batch on their own thread and
are flushed every `flush_s`.

Every sink is optional: it reads its credentials from the environment and reports
`enabled = False` (and is not even given a thread) when they are missing.

Hooking it up (see WIRING in the integration notes):
    fan = build_fanout(cfg, sampler)     # reads cfg["integrations"]; polls the sampler, no edits there
    fan.start()
    fan.on_scan(rec, image_path)         # after store.save()
    fan.status()                         # for /api/status
    fan.close()
"""
from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
from typing import Any, Iterable

log = logging.getLogger(__name__)

ENV_FIELDS = ("mq2_v", "gas_ohms", "temp_c", "rh", "pressure_hpa")


# ---------------------------------------------------------------- interface
class Sink:
    """Override what you need. All methods run on the sink's own thread, except status()."""

    name = "sink"
    enabled = False
    wants_readings = True   # False = the fan-out never queues env samples for this sink
    wants_scans = True
    flush_s = 5.0           # flush() cadence
    queue_size = 2000

    def start(self) -> None:
        """Called once on the sink thread before the first item (connect lazily here)."""

    def on_reading(self, sample: dict) -> None:
        """One env sample (see reading_dict). Buffer it; send in flush()."""

    def on_scan(self, rec: dict) -> None:
        """One scan record as saved by Store (plus rover_id and image_path)."""

    def flush(self) -> None:
        """Send whatever is buffered. Raise on failure; keep the buffer bounded yourself."""

    def close(self) -> None:
        """Last chance to flush and release connections."""

    def status(self) -> dict:
        return {"enabled": self.enabled}


# ---------------------------------------------------------------- helpers
def _clean(x: Any) -> Any:
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def reading_dict(r: Any, rover_id: str, sampler: Any = None) -> dict:
    """EnvReading (or dict) -> flat sample. voc_top/voc_bottom are ln(baseline/now) in BME680
    units (plant side / background), the same numbers LeafLink gets; None until warm."""
    get = r.get if isinstance(r, dict) else (lambda k, d=None: getattr(r, k, d))
    out = {"t": float(get("t", time.time())), "rover_id": rover_id}
    for k in ENV_FIELDS:
        out[k] = _clean(get(k))
    out["voc_top"] = _clean(get("voc_top"))
    out["voc_bottom"] = _clean(get("voc_bottom"))
    out["warm"] = get("warm")
    out["sniffing"] = get("sniffing")
    if sampler is not None:
        try:
            if out["voc_top"] is None and out["voc_bottom"] is None:
                top, bottom = sampler.voc_pair()
                out["voc_top"], out["voc_bottom"] = _clean(top), _clean(bottom)
            out["warm"] = bool(sampler.warm)
            out["sniffing"] = bool(getattr(sampler, "_sniffing", 0))
        except Exception:  # sampler mid-startup; the raw values are still worth keeping
            pass
    return out


def _section(cfg: Any, key: str) -> dict:
    if cfg is None:
        return {}
    v = cfg.get(key) if hasattr(cfg, "get") else getattr(cfg, key, None)
    if v is None:
        return {}
    if hasattr(v, "to_dict"):
        return v.to_dict()
    return dict(v) if isinstance(v, dict) else {}


# ---------------------------------------------------------------- runner
class _Runner:
    """One sink, one bounded queue, one daemon thread."""

    def __init__(self, sink: Sink):
        self.sink = sink
        self.q: queue.Queue = queue.Queue(maxsize=int(getattr(sink, "queue_size", 2000)))
        self.dropped = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_ok: float | None = None
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name=f"sink-{sink.name}", daemon=True)

    def submit(self, kind: str, payload: dict) -> None:
        try:
            self.q.put_nowait((kind, payload))
        except queue.Full:
            self.dropped += 1  # offline for a while: shed load, never block the caller

    def _call(self, fn, *a) -> None:
        try:
            fn(*a)
            self.last_ok = time.time()
        except Exception as e:  # a sink must never kill its thread
            self.errors += 1
            msg = f"{type(e).__name__}: {e}"[:300]
            if msg != self.last_error:
                log.warning("sink %s: %s", self.sink.name, msg)
            self.last_error = msg

    def _loop(self) -> None:
        self._call(self.sink.start)
        flush_s = max(0.05, float(self.sink.flush_s))
        next_flush = time.monotonic() + flush_s
        while not self._stop.is_set():
            try:
                kind, payload = self.q.get(timeout=max(0.0, next_flush - time.monotonic()))
                self._call(self.sink.on_scan if kind == "scan" else self.sink.on_reading, payload)
            except queue.Empty:
                pass
            if time.monotonic() >= next_flush:
                self._call(self.sink.flush)
                next_flush = time.monotonic() + flush_s
        # drain what's left, then one final flush + close
        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            self._call(self.sink.on_scan if kind == "scan" else self.sink.on_reading, payload)
        self._call(self.sink.flush)
        self._call(self.sink.close)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout)

    def status(self) -> dict:
        try:
            s = dict(self.sink.status())
        except Exception as e:
            s = {"status_error": str(e)}
        s.update(queued=self.q.qsize(), dropped=self.dropped, errors=self.errors,
                 error=self.last_error, last_ok=self.last_ok)
        return s


# ---------------------------------------------------------------- fan-out
class FanOut:
    def __init__(self, sinks: Iterable[Sink], rover_id: str = "rover-1"):
        self.rover_id = rover_id
        self.sinks = list(sinks)
        self.runners = [_Runner(s) for s in self.sinks if s.enabled]
        self._sampler = None
        self._poll_hz = 10.0
        self._running = False

    @property
    def enabled(self) -> bool:
        return bool(self.runners)

    # ---- lifecycle
    def attach_sampler(self, sampler: Any, hz: float = 10.0) -> "FanOut":
        """Forward every new sampler reading (polls sampler.latest(), so sampler.py is untouched)."""
        self._sampler, self._poll_hz = sampler, float(hz)
        return self

    def start(self) -> "FanOut":
        if not self.runners:
            log.info("telemetry sinks: none enabled")
            return self
        self._running = True
        for r in self.runners:
            r.thread.start()
        if self._sampler is not None and any(r.sink.wants_readings for r in self.runners):
            threading.Thread(target=self._poll, name="sink-poll", daemon=True).start()
        log.info("telemetry sinks: %s", ", ".join(r.sink.name for r in self.runners))
        return self

    def close(self) -> None:
        self._running = False
        for r in self.runners:
            r.stop()

    # ---- inputs (cheap, non-blocking, safe from any thread)
    def on_reading(self, r: Any) -> None:
        if not self.runners:
            return
        sample = reading_dict(r, self.rover_id, None if isinstance(r, dict) else self._sampler)
        for run in self.runners:
            if run.sink.wants_readings:
                run.submit("reading", sample)

    def on_scan(self, rec: dict, image_path: str | None = None) -> None:
        if not self.runners:
            return
        payload = {**rec, "rover_id": rec.get("rover_id") or self.rover_id}
        if image_path:
            payload["image_path"] = image_path
        for run in self.runners:
            if run.sink.wants_scans:
                run.submit("scan", payload)

    def status(self) -> dict:
        out = {s.name: {"enabled": False} for s in self.sinks}
        out.update({r.sink.name: r.status() for r in self.runners})
        return out

    def sink(self, name: str) -> Sink | None:
        return next((s for s in self.sinks if s.name == name), None)

    # ---- sampler poller
    def _poll(self) -> None:
        period = 1.0 / max(self._poll_hz, 0.1)
        last_t = None
        while self._running:
            t0 = time.monotonic()
            try:
                r = self._sampler.latest()
                if r is not None and r.t != last_t:
                    last_t = r.t
                    self.on_reading(r)
            except Exception as e:
                log.debug("sink poll: %s", e)
            time.sleep(max(0.0, period - (time.monotonic() - t0)))


# ---------------------------------------------------------------- factory
def build_fanout(cfg: Any = None, sampler: Any = None, store_root: str | None = None) -> FanOut:
    """cfg = the whole plantbot config (uses cfg.integrations), or None for env-only defaults.

    Never raises: a sink whose optional dependency is missing is logged and left disabled."""
    icfg = _section(cfg, "integrations")
    ll = _section(cfg, "leaflink")
    rover_id = os.getenv("PLANTBOT_ROVER_ID") or icfg.get("rover_id") or ll.get("device") or "rover-1"
    sinks: list[Sink] = []
    if icfg.get("enabled", True):
        for key, mod, cls in (("tigerdata", "tigerdata", "TigerDataSink"),
                              ("snowflake", "snowflake_sink", "SnowflakeSink"),
                              ("solana", "solana_log", "SolanaCertSink")):
            sub = icfg.get(key) or {}
            if not sub.get("enabled", True):
                continue
            try:
                m = __import__(f"plantbot.integrations.{mod}", fromlist=[cls])
                kw = {"store_root": store_root} if key == "solana" else {}
                sinks.append(getattr(m, cls)(sub, rover_id=rover_id, **kw))
            except Exception as e:
                log.warning("sink %s unavailable: %s", key, e)
    fan = FanOut(sinks, rover_id=rover_id)
    if sampler is not None:
        hz = icfg.get("reading_hz")
        fan.attach_sampler(sampler, float(hz) if hz else float(getattr(getattr(sampler, "cfg", None), "sample_hz", 10)))
    return fan
