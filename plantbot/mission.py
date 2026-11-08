"""Mission state machine. Owns the robot: the web layer only sends it commands.

BOOT -> WARMUP -> IDLE
IDLE --scan--> STOP_SETTLE -> CAPTURE_SNIFF -> ANALYZE -> REPORT -> IDLE
IDLE --auto--> CRUISE -> APPROACH -> (scan) -> PASS_PLANT -> CRUISE ... -> IDLE
any  --estop--> ESTOP --reset--> IDLE
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from plantbot import fusion
from plantbot.sensing import turgor
from plantbot.vision.isolate import isolate

log = logging.getLogger(__name__)

DRIVABLE = {"WARMUP", "IDLE"}  # states where the manual drive pad works
BASELINE_TRACKING = {"WARMUP", "IDLE", "CRUISE"}


@dataclass
class Robot:
    cfg: Any
    motors: Any
    camera: Any
    env: Any
    sampler: Any
    ranger: Any
    fan: Any
    classifier: Any
    store: Any
    devices: dict = field(default_factory=dict)
    leaflink: Any = None
    sinks: Any = None  # plantbot.integrations.sinks.FanOut
    tactile: Any = None
    arm: Any = None
    guard: Any = None
    zelos: Any = None

    def close(self) -> None:
        for d in (self.zelos, self.arm, self.sinks, self.motors, self.camera, self.sampler, self.leaflink, self.tactile):
            if d is None:
                continue
            try:
                d.close()
            except Exception:
                log.exception("close failed")


class Mission:
    def __init__(self, robot: Robot):
        self.r = robot
        self.cfg = robot.cfg.mission
        self.state = "BOOT"
        self.mode = "manual"
        self.error: str | None = None
        self.last_scan: dict | None = None
        self.range_cm: float | None = None
        self.auto_scans = 0
        self.task: asyncio.Task | None = None  # current scan or auto run
        self.cancel = threading.Event()  # tells sniff threads to bail on E-stop
        self.broadcast: Callable[[dict], Awaitable[None]] = self._no_broadcast
        self._drive_refused_at = 0.0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.last_turgor: dict | None = None

    @staticmethod
    async def _no_broadcast(msg: dict) -> None:
        pass

    def set_state(self, s: str) -> None:
        if s != self.state:
            log.info("state %s -> %s", self.state, s)
            self.state = s
        self.r.sampler.track_baseline = s in BASELINE_TRACKING

    # ---------- lifecycle ----------
    async def run(self) -> None:
        """Background tick: warm-up transition and range polling."""
        self.loop = asyncio.get_running_loop()
        self.set_state("WARMUP")
        while True:
            try:
                # off the event loop: a stuck sensor read must not freeze the web server
                self.range_cm = await asyncio.to_thread(self.r.ranger.distance_cm)
            except Exception as e:
                log.warning("ranger read failed: %s", e)
                self.range_cm = None
            if self.state == "WARMUP" and self.r.sampler.warm:
                self.set_state("IDLE")
            await asyncio.sleep(0.1)

    # ---------- commands ----------
    async def handle(self, msg: dict) -> None:
        if "drive" in msg:
            l, r = msg["drive"]
            if self.state in DRIVABLE and self.mode == "manual":
                self.r.motors.drive(float(l), float(r))
            elif (l or r) and time.monotonic() - self._drive_refused_at > 2:  # held buttons resend at 10 Hz
                self._drive_refused_at = time.monotonic()
                log.warning("drive ignored: state=%s mode=%s (drive works in %s, manual mode)",
                            self.state, self.mode, "/".join(sorted(DRIVABLE)))
            return
        cmd = msg.get("cmd")
        if cmd == "estop":
            await self.estop("dashboard")
        elif cmd == "reset":
            if self.state == "ESTOP":
                self.error = None
                self.set_state("IDLE" if self.r.sampler.warm else "WARMUP")
        elif cmd == "scan":
            if self.state in DRIVABLE and not self._busy():
                self.task = asyncio.create_task(self._manual_scan())
        elif cmd == "auto_on":
            if self.r.devices.get("ranger") not in ("ok", "mock"):
                log.warning("auto refused: no rangefinder, it would drive blind")
            elif self.state in DRIVABLE and not self._busy():
                self.task = asyncio.create_task(self._auto())
        elif cmd == "auto_off":
            if self.mode == "auto":
                self._cancel_task()
                self.r.motors.stop()
                self.mode = "manual"
                self.set_state("IDLE")
        elif cmd == "recalibrate":
            self.r.sampler.recalibrate()
        elif cmd == "mock_spike":
            spike = getattr(self.r.env, "spike", None)
            if spike:
                spike()
        elif cmd == "probe":  # touch-only: press the leaf, report turgor, no photo/sniff
            if self.state in DRIVABLE and not self._busy():
                self.task = asyncio.create_task(self._probe_only())
        elif cmd == "turgor_cal":  # {"cmd":"turgor_cal","kind":"turgid"|"wilted"}: uses the last probe
            if self.last_turgor and self.last_turgor.get("pressure_at_ref") is not None:
                turgor.calibrate(msg.get("kind", "turgid"), turgor.TurgorResult(**self.last_turgor), self._tcfg())
        elif cmd == "mock_bump":
            poke = getattr(self.r.tactile, "poke", None)
            if poke:
                poke()
        elif cmd == "mock_leaf":  # {"cmd":"mock_leaf","turgor":0.2}
            set_leaf = getattr(self.r.tactile, "set_leaf", None)
            if set_leaf:
                set_leaf(float(msg.get("turgor", 0.8)))
        else:
            log.warning("unknown message %s", msg)

    def _busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def _cancel_task(self) -> None:
        self.cancel.set()
        if self._busy():
            self.task.cancel()

    async def estop(self, why: str) -> None:
        log.warning("E-STOP (%s)", why)
        self._cancel_task()
        self.r.motors.stop()
        self.r.fan.off()
        self.mode = "manual"
        self.error = f"E-STOP: {why}"
        self.set_state("ESTOP")

    # ---------- touch (SensX25) ----------
    def _tcfg(self):
        t = self.r.cfg.get("tactile")
        return t.get("turgor") if t is not None else None

    def _touch_enabled(self) -> bool:
        tc = self._tcfg()
        return (self.r.tactile is not None and getattr(self.r.arm, "kind", "none") != "none"
                and self.r.devices.get("tactile") in ("ok", "mock") and bool(tc is None or tc.get("enabled", True)))

    def _touch(self) -> dict:
        """Worker thread: press the leaf, return the TurgorResult as a dict."""
        res = turgor.probe(self.r.tactile, self.r.arm, self._tcfg(), motors=self.r.motors, guard=self.r.guard,
                           cancel=self.cancel)
        self.last_turgor = res.to_dict()
        return self.last_turgor

    async def _probe_only(self) -> None:
        self.cancel = threading.Event()
        prev = self.state
        try:
            self.set_state("PROBE")
            self.r.motors.stop()
            res = await asyncio.to_thread(self._touch)
            await self.broadcast({"turgor": res})
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("probe failed")
            self.error = f"probe failed: {e}"
        finally:
            if self.state == "PROBE":
                self.set_state(prev if prev in DRIVABLE else "IDLE")

    def on_bump_trip(self, e: dict) -> None:
        """Tactile reader thread, at the moment of contact (motors already stopped by the guard)."""
        if self.loop and self.mode == "auto":
            asyncio.run_coroutine_threadsafe(self.estop("bumper contact"), self.loop)

    def on_bump(self, e: dict) -> None:
        """Tactile reader thread, on release: log to Zelos and the dashboard."""
        if self.r.zelos:
            self.r.zelos.publish_bump(e)
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.broadcast({"bump": e}), self.loop)

    # ---------- scan ----------
    def _vision(self) -> dict:
        """Runs in a worker thread: capture -> isolate -> classify."""
        raw = self.r.camera.capture()
        iso = isolate(raw, self.r.cfg.vision)
        pred = self.r.classifier.predict(iso.crop)
        return {"raw": raw, "iso": iso, "pred": pred}

    async def _scan(self, mode: str) -> dict:
        self.cancel = threading.Event()
        scan_id = self.r.store.new_id()
        t = time.time()
        self.set_state("STOP_SETTLE")
        self.r.motors.stop()
        await asyncio.sleep(self.cfg.settle_s)

        self.set_state("CAPTURE_SNIFF")
        sniff = asyncio.create_task(asyncio.to_thread(self.r.sampler.sniff, self.r.fan, cancel=self.cancel))
        try:
            vis = await asyncio.to_thread(self._vision)
        except BaseException:
            self.cancel.set()
            raise
        await self.broadcast({"scan_progress": {"id": scan_id, "label": vis["pred"].label,
                                                "p_healthy": vis["pred"].p_healthy}})
        # touch after the photo (so the arm isn't in it), during the sniff window
        touch = asyncio.create_task(asyncio.to_thread(self._touch)) if self._touch_enabled() else None
        voc = await sniff
        try:
            touch_res = await touch if touch else None
        except Exception as e:
            log.warning("turgor probe failed: %s", e)
            touch_res = None

        self.set_state("ANALYZE")
        pred, iso = vis["pred"], vis["iso"]
        fused = fusion.fuse(pred.p_healthy, voc["mq2_ratio"], voc["gas_ratio"], self.r.cfg.fusion,
                            diff_ratio=voc["diff_ratio"] if voc["diff_calibrated"] else None)
        if touch is not None:
            fused = turgor.fuse_with_turgor(fused, touch_res, self._tcfg(), self.r.cfg.fusion)
        rec = {
            "id": scan_id, "t": t, "mode": mode,
            "label": pred.label, "p_healthy": round(pred.p_healthy, 4), "probs": pred.probs,
            "mq2_ratio": voc["mq2_ratio"], "gas_ratio": voc["gas_ratio"], "diff_ratio": voc["diff_ratio"],
            "temp_c": voc["temp_c"], "rh": voc["rh"],
            "health": fused["health"], "status": fused["status"], "voc_stress": fused["voc_stress"],
            "explain": fused["explain"],
            "isolation": iso.method, "bbox": iso.bbox, "blob_frac": iso.blob_frac,
            "infer_ms": round(pred.ms, 1), "backend": pred.backend,
            "voc": voc, "range_cm": self.range_cm,
            "turgor_index": fused.get("turgor_index"), "turgor": touch_res,
        }
        self.set_state("REPORT")
        rec = await asyncio.to_thread(self.r.store.save, rec, vis["raw"], iso.mask_vis, iso.crop)
        self.last_scan = rec
        if self.r.leaflink:
            self.r.leaflink.submit_scan(scan_id, os.path.join(self.r.store.root, scan_id, "raw.jpg"), t)
        if self.r.sinks:
            self.r.sinks.on_scan(rec, os.path.join(self.r.store.root, scan_id, "raw.jpg"))
        if self.r.zelos:
            self.r.zelos.publish_scan(rec)
        log.info("scan %s: %s %.0f (%s)", scan_id, rec["status"], rec["health"], rec["explain"])
        await self.broadcast({"scan": rec})
        return rec

    async def _manual_scan(self) -> None:
        try:
            self.error = None
            await self._scan("manual")
            self.set_state("IDLE" if self.r.sampler.warm else "WARMUP")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("scan failed")
            self.r.motors.stop()
            self.error = f"scan failed: {e}"
            self.set_state("IDLE")

    # ---------- auto (Should): untested on hardware ----------
    async def _drive_until(self, speed: float, done: Callable[[float | None], bool], timeout: float) -> bool:
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            if done(self.range_cm):
                return True
            self.r.motors.drive(speed, speed)  # re-sent every tick, so the watchdog stays fed
            await asyncio.sleep(0.1)
        self.r.motors.stop()
        return False

    async def _auto(self) -> None:
        c = self.cfg
        self.mode, self.auto_scans, self.error = "auto", 0, None
        t_end = time.monotonic() + c.auto_timeout_s
        try:
            while self.auto_scans < c.auto_max_scans and time.monotonic() < t_end:
                self.set_state("CRUISE")
                if not await self._drive_until(c.cruise_speed, lambda d: d is not None and d < c.approach_cm,
                                               t_end - time.monotonic()):
                    break  # end of row / timeout
                self.set_state("APPROACH")
                await self._drive_until(c.approach_speed, lambda d: d is not None and d <= c.stop_cm, 5.0)
                await self._scan("auto")
                self.auto_scans += 1
                self.set_state("PASS_PLANT")
                await self._drive_until(c.cruise_speed, lambda d: d is None or d > c.clear_cm, c.pass_timeout_s)
            self.r.motors.stop()
            self.mode = "manual"
            self.set_state("IDLE")
        except asyncio.CancelledError:
            self.r.motors.stop()
        except Exception as e:
            log.exception("auto failed")
            await self.estop(f"auto error: {e}")

    # ---------- telemetry ----------
    def _tactile_telemetry(self) -> dict | None:
        t = self.r.tactile
        if t is None or not hasattr(t, "status"):
            return None
        st = t.status()
        f = t.latest()
        st["grid"] = None if f is None else f.grid.astype(int).tolist()  # 25 ints for a dashboard heatmap
        return st

    def telemetry(self) -> dict:
        s = self.r.sampler
        with s.lock:
            env = dict(s.smooth)
            base = {"mq2_rs_kohm": s.base_rs, "gas_ohms": s.base_gas}
        return {
            "state": self.state, "mode": self.mode, "mock": bool(self.r.cfg.mock_mode),
            "warm": s.warm, "warmup_remaining_s": round(s.warmup_remaining(), 1),
            "env": env, "ratios": s.ratios(), "baseline": base,
            "diff_calibrated": s.crosscal.calibrated, "plant_sensor": s.plant_sensor,
            "range_cm": None if self.range_cm is None else round(self.range_cm, 1),
            "motors": [round(self.r.motors.left, 2), round(self.r.motors.right, 2)],
            "watchdog_trips": self.r.motors.watchdog_trips,
            "devices": self.r.devices, "error": self.error, "auto_scans": self.auto_scans,
            "leaflink": self.r.leaflink.status() if self.r.leaflink else None,
            "sinks": self.r.sinks.status() if self.r.sinks else None,
            "tactile": self._tactile_telemetry(),
            "bumper": self.r.guard.status() if self.r.guard else None,
            "last_turgor": None if not self.last_turgor else {k: self.last_turgor.get(k) for k in
                                                               ("turgor_index", "label", "peak_force", "flags")},
            "t": time.time(),
        }
