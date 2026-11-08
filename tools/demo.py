"""Scripted demo: the real dashboard on a laptop, driving a fake row of plants with fixed results.

    python tools/demo.py              # dashboard at http://localhost:8000/, demo starts after warm-up
    python tools/demo.py --loop       # replay the row forever (expo table)
    python tools/demo.py --manual     # stage the row but wait for you to press Auto on the dashboard

Everything is --mock underneath (no hardware). On top of that, the plants are scripted
(PLANTS below): the camera shows each plant growing as the rover approaches, the classifier
returns the scripted label, and sick plants get a VOC puff while the rover approaches them,
so every run tells the same story. Scans go to scans-demo/, wiped on each start.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plantbot import config  # noqa: E402
from plantbot.hal.camera import _ThreadedCamera, synth_plant  # noqa: E402
from plantbot.vision.classify import Prediction  # noqa: E402

log = logging.getLogger("demo")

# The row, in driving order. seed picks the synthetic photo (seed % 3: 0 healthy, 1 spots, 2 yellowing).
# voc: plant-local smell during the approach (mq2_dv volts up, gas_scale resistance multiplier), None = clean air.
PLANTS = [
    {"name": "basil", "seed": 0, "label": "healthy", "p_healthy": 0.95, "voc": None},
    {"name": "tomato", "seed": 3, "label": "healthy", "p_healthy": 0.91, "voc": None},
    {"name": "pepper", "seed": 1, "label": "leaf_spot", "p_healthy": 0.38, "voc": {"mq2_dv": 0.04, "gas_scale": 0.94}},
    {"name": "tomato", "seed": 2, "label": "blight", "p_healthy": 0.07, "voc": {"mq2_dv": 0.45, "gas_scale": 0.55}},
    {"name": "basil", "seed": 6, "label": "healthy", "p_healthy": 0.88, "voc": None},
]
LABELS = ["healthy", "leaf_spot", "blight"]


class Row:
    """Which scripted plant is in front of the rover, from the mock rangefinder's odometry."""

    def __init__(self, ranger):
        self.ranger = ranger

    def index(self) -> int:
        return int(self.ranger.pos // self.ranger.spacing)

    def plant(self) -> dict | None:
        i = self.index()
        return PLANTS[i] if 0 <= i < len(PLANTS) else None

    def ahead_cm(self) -> float:
        return self.ranger.spacing - (self.ranger.pos % self.ranger.spacing)

    def reset(self) -> None:
        self.ranger.pos, self.ranger.t = 10.0, time.monotonic()


class DemoCamera(_ThreadedCamera):
    """The current plant, scaled by distance: small and far while cruising, frame-filling at the stop."""

    def __init__(self, cfg, row: Row, stop_cm: float):
        super().__init__(cfg)
        self.row, self.stop_cm = row, stop_cm
        self.photos = {p["seed"]: synth_plant(cfg.width, cfg.height, seed=p["seed"]) for p in PLANTS}
        self.empty = cv2.GaussianBlur(synth_plant(cfg.width, cfg.height, seed=99), (0, 0), 25)

    def _grab(self) -> np.ndarray:
        p = self.row.plant()
        if p is None:
            return self.empty.copy()
        photo = self.photos[p["seed"]]
        h, w = photo.shape[:2]
        s = float(np.clip(self.stop_cm / max(self.row.ahead_cm(), 1.0), 0.3, 1.0))
        if s >= 0.999:
            return photo.copy()
        frame = cv2.GaussianBlur(photo, (0, 0), 18)  # out-of-focus surroundings
        sw, sh = int(w * s), int(h * s)
        small = cv2.resize(photo, (sw, sh), interpolation=cv2.INTER_AREA)
        x, y = (w - sw) // 2, (h - sh) // 2 + int((1 - s) * h * 0.15)
        frame[y:y + sh, x:x + sw] = small
        return frame

    def _lores(self, rgb: np.ndarray) -> np.ndarray:
        img = super()._lores(rgb).copy()
        p = self.row.plant()
        tag = f"row 1 / plant {self.row.index() + 1}  {self.row.ahead_cm():.0f} cm" if p else "end of row"
        cv2.putText(img, f"{tag}  {time.strftime('%H:%M:%S')}", (10, img.shape[0] - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        return img


class ScriptedClassifier:
    def __init__(self, row: Row):
        self.row = row

    def predict(self, crop_rgb: np.ndarray) -> Prediction:
        t0 = time.perf_counter()
        time.sleep(0.18)  # about what the TFLite model takes on the Pi 4
        p = self.row.plant() or PLANTS[0]
        rest = 1 - p["p_healthy"]
        sick = p["label"] if p["label"] != "healthy" else "leaf_spot"
        probs = {"healthy": p["p_healthy"]}
        for l in LABELS[1:]:
            probs[l] = rest * (0.8 if l == sick else 0.2)
        return Prediction(max(probs, key=probs.get), p["p_healthy"], probs, "demo", (time.perf_counter() - t0) * 1000)


async def director(mission, row: Row, loop: bool, manual: bool) -> None:
    def say(msg: str) -> None:
        log.info("\033[1;32m▶ %s\033[0m", msg)

    while True:
        while mission.state != "IDLE":
            await asyncio.sleep(0.2)
        row.reset()
        if manual:
            say(f"row staged: {len(PLANTS)} plants. Press Auto on the dashboard.")
            while mission.mode != "auto":
                await asyncio.sleep(0.2)
        else:
            say(f"sensors warm. Starting autonomous run down a row of {len(PLANTS)} plants in 3 s")
            await asyncio.sleep(3)
            await mission.handle({"cmd": "auto_on"})
            while mission.mode != "auto":  # auto_on starts a task; wait for it to take over
                await asyncio.sleep(0.05)
        puffed, seen = set(), None
        while mission.mode == "auto" or mission.state != "IDLE":
            i, p = row.index(), row.plant()
            if mission.state == "APPROACH" and p and p["voc"] and i not in puffed:
                puffed.add(i)
                mission.r.env.spike(duration=14, **p["voc"])
            if mission.state != seen:
                seen = mission.state
                if p and seen in ("APPROACH", "CAPTURE_SNIFF"):
                    say({"APPROACH": f"plant {i + 1} ({p['name']}) ahead, slowing down",
                         "CAPTURE_SNIFF": f"photo + VOC sniff on plant {i + 1}"}[seen])
            await asyncio.sleep(0.1)
        scans = mission.r.store.list(len(PLANTS))
        say("run done: " + ", ".join(f"{s['status']} {s['health']:.0f}" for s in reversed(scans)))
        if not loop:
            say("demo finished; dashboard stays up (Ctrl-C to quit, --loop to repeat)")
            return
        await asyncio.sleep(8)


def main() -> None:
    ap = argparse.ArgumentParser(prog="demo")
    ap.add_argument("--loop", action="store_true", help="repeat the run forever")
    ap.add_argument("--manual", action="store_true", help="wait for the Auto button instead of starting by itself")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")

    cfg = config.load(mock=True)
    cfg.store.dir = str(config.ROOT / "scans-demo")
    shutil.rmtree(cfg.store.dir, ignore_errors=True)
    cfg.mock.env_script = []  # no room-wide event: only the scripted plants smell
    cfg.camera.driver = "folder"
    cfg.mission.auto_max_scans = len(PLANTS)
    cfg.mission.auto_timeout_s = 600

    from plantbot.__main__ import build_robot
    from plantbot.mission import Mission
    from plantbot.web.server import make_app

    robot = build_robot(cfg)
    row = Row(robot.ranger)
    robot.camera.close()
    robot.camera = DemoCamera(cfg.camera, row, cfg.mission.stop_cm).start()
    robot.classifier = ScriptedClassifier(row)
    robot.devices["classifier"] = "demo"

    mission = Mission(robot)
    robot.guard.on_trip = mission.on_bump_trip
    robot.guard.on_event = mission.on_bump
    robot.zelos.mission = mission
    robot.zelos.start()
    app = make_app(mission)

    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app_):
        async with inner(app_):
            task = asyncio.create_task(director(mission, row, a.loop, a.manual))
            yield
            task.cancel()

    app.router.lifespan_context = lifespan
    log.info("demo dashboard: http://localhost:%d/", a.port)
    import uvicorn

    try:
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    finally:
        robot.motors.stop()
        robot.close()


if __name__ == "__main__":
    main()
