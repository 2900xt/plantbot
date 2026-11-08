"""plantbot/recorder.py: wheel-press photo sessions (no camera or mouse needed)."""
import asyncio
import json
import time

import numpy as np

from plantbot.config import Cfg
from plantbot.recorder import Recorder


class FakeCam:
    """latest() like _ThreadedCamera: a new frame each call unless `frozen` (camera dropped out)."""

    def __init__(self):
        self.frozen = False
        self.n = 0
        self.frame = None
        self.at = 0.0

    def latest(self):
        if not self.frozen:
            self.n += 1
            self.frame = np.full((48, 64, 3), self.n % 255, np.uint8)
            self.at = time.monotonic()
        return (self.frame.copy(), self.at)


def cfg(tmp_path, **kw):
    return Cfg(enabled=True, interval_s=0.05, lockout_s=0.3, max_frame_age_s=0.2, out_dir=str(tmp_path), **kw)


async def _record(rec, seconds):
    task = asyncio.create_task(rec.run())
    rec.toggle(100.0)
    await asyncio.sleep(seconds)
    rec.toggle(101.0)
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_session_photos_and_marker(tmp_path):
    rec = Recorder(FakeCam(), cfg(tmp_path))
    asyncio.run(_record(rec, 0.32))
    (session,) = [p for p in tmp_path.iterdir() if p.is_dir()]
    jpgs = sorted(session.glob("*.jpg"))
    marker = json.loads((session / "session.json").read_text())
    assert len(jpgs) >= 4 and marker["photos"] == len(jpgs) and marker["stopped_by"] == "wheel"
    assert jpgs[0].name == "0001.jpg" and not list(session.glob(".*.tmp"))
    assert len({p.read_bytes() for p in jpgs}) == len(jpgs)  # every photo is a different frame


def test_stale_frames_are_skipped_not_saved(tmp_path):
    cam = FakeCam()
    rec = Recorder(cam, cfg(tmp_path))

    async def go():
        task = asyncio.create_task(rec.run())
        rec.toggle(100.0)
        await asyncio.sleep(0.12)
        cam.frozen = True            # camera stops delivering
        await asyncio.sleep(0.4)
        cam.frozen = False           # and comes back
        await asyncio.sleep(0.12)
        rec.toggle(101.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    (session,) = [p for p in tmp_path.iterdir() if p.is_dir()]
    jpgs = sorted(session.glob("*.jpg"))
    marker = json.loads((session / "session.json").read_text())
    assert marker["skipped"] >= 3 and marker["photos"] == len(jpgs)
    assert len({p.read_bytes() for p in jpgs}) == len(jpgs)  # no repeated (stale) frame


def test_bounce_lockout(tmp_path):
    rec = Recorder(FakeCam(), cfg(tmp_path))
    assert rec.toggle(10.0) == "started"
    assert rec.toggle(10.12) == "ignored" and rec.recording   # bounce right after start
    assert rec.toggle(12.0) == "stopped"
    assert rec.toggle(12.17) == "ignored" and not rec.recording  # bounce right after stop
    assert rec.toggle(13.0) == "started"


def test_refuses_synthetic_fallback_camera(tmp_path):
    rec = Recorder(FakeCam(), cfg(tmp_path), camera_status="offline")
    assert rec.toggle(1.0) == "refused" and not rec.recording
    assert not any(tmp_path.iterdir())


def test_shutdown_closes_session(tmp_path):
    rec = Recorder(FakeCam(), cfg(tmp_path))

    async def go():
        task = asyncio.create_task(rec.run())
        rec.toggle(1.0)
        await asyncio.sleep(0.15)
        task.cancel()                # service stopping mid-recording
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    (session,) = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert json.loads((session / "session.json").read_text())["stopped_by"] == "shutdown"
