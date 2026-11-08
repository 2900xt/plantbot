"""plantbot/hal/mouse_drive.py with a fake evdev: no Pi, no mouse needed."""
import asyncio
import errno
import sys
import types

import pytest

from plantbot.config import Cfg
from plantbot.hal import mouse_drive

EV_KEY, EV_REL = 1, 2
CODES = {"BTN_LEFT": 0x110, "BTN_RIGHT": 0x111, "BTN_MIDDLE": 0x112, "BTN_SIDE": 0x113, "BTN_EXTRA": 0x114}
L, R, MID = CODES["BTN_LEFT"], CODES["BTN_RIGHT"], CODES["BTN_MIDDLE"]


class Ev:
    def __init__(self, t, c, v):
        self.type, self.code, self.value = t, c, v

    def timestamp(self):
        return 0.0


class FakeMouse:
    def __init__(self, path, script=(), grab_error=None, keys=(L, R, MID)):
        self.path, self.name, self.script = path, "Fake Mouse", list(script)
        self.grab_error, self.keys, self.closed = grab_error, list(keys), False

    def capabilities(self):
        return {EV_KEY: self.keys, EV_REL: [0, 1]}

    def grab(self):
        if self.grab_error:
            raise OSError(self.grab_error, "busy")

    def close(self):
        self.closed = True

    async def async_read_loop(self):
        for delay, t, c, v in self.script:
            await asyncio.sleep(delay)
            yield Ev(t, c, v)
        await asyncio.sleep(3600)


class FakeMission:
    def __init__(self, recorder=None):
        self.r = types.SimpleNamespace(devices={}, recorder=recorder)
        self.drives = []

    async def handle(self, msg):
        self.drives.append(tuple(msg["drive"]))


def cfg():
    return Cfg(enabled=True, device=None, grab=True, speed=1.0,
               buttons=Cfg(left_fwd="BTN_LEFT", left_back="BTN_EXTRA", right_fwd="BTN_RIGHT",
                           right_back="BTN_SIDE", record="BTN_MIDDLE"))


@pytest.fixture
def fake_evdev(monkeypatch):
    """devs: path -> FakeMouse (or an exception to raise on open). readable: paths the user may open."""
    state = types.SimpleNamespace(devs={}, readable=None)

    def open_dev(path):
        d = state.devs[path]
        if isinstance(d, Exception):
            raise d
        return d

    mod = types.ModuleType("evdev")
    mod.ecodes = types.SimpleNamespace(EV_KEY=EV_KEY, EV_REL=EV_REL, BTN_LEFT=L, BTN_RIGHT=R, ecodes=CODES)
    mod.InputDevice = open_dev
    monkeypatch.setitem(sys.modules, "evdev", mod)
    monkeypatch.setattr(mouse_drive.glob, "glob", lambda pattern: sorted(state.devs))
    monkeypatch.setattr(mouse_drive.os, "access",
                        lambda p, mode: state.readable is None or p in state.readable)
    monkeypatch.setattr(mouse_drive, "RETRY_S", 0.01)
    return state


async def _run_for(mission, seconds):
    task = asyncio.create_task(mouse_drive.run(mission, cfg()))
    await asyncio.sleep(seconds)
    assert not task.done(), task.exception()  # the reader must never die
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_buttons_drive_each_wheel(fake_evdev):
    fake_evdev.devs["/dev/input/event0"] = FakeMouse("/dev/input/event0", [
        (0.01, EV_REL, 0, 5), (0.01, EV_KEY, L, 1), (0.15, EV_KEY, R, 1), (0.15, EV_KEY, L, 0), (0.15, EV_KEY, R, 0)])
    m = FakeMission()
    asyncio.run(_run_for(m, 0.6))
    assert m.r.devices["mouse"] == "ok"
    assert (1.0, 0.0) in m.drives and (1.0, 1.0) in m.drives and (0.0, 1.0) in m.drives
    assert m.drives[-1] == (0.0, 0.0)


def test_skips_non_mouse_nodes(fake_evdev):
    fake_evdev.devs["/dev/input/event0"] = FakeMouse("/dev/input/event0", keys=[30, 31])  # keyboard
    fake_evdev.devs["/dev/input/event1"] = OSError(errno.ENODEV, "unplugged")  # vanished mid-scan
    fake_evdev.devs["/dev/input/event2"] = FakeMouse("/dev/input/event2", [(0.01, EV_KEY, L, 1)])
    m = FakeMission()
    asyncio.run(_run_for(m, 0.2))
    assert m.r.devices["mouse"] == "ok" and (1.0, 0.0) in m.drives


def test_reports_no_permission(fake_evdev):
    fake_evdev.devs["/dev/input/event0"] = FakeMouse("/dev/input/event0")
    fake_evdev.readable = set()  # service user not in the input group
    m = FakeMission()
    asyncio.run(_run_for(m, 0.1))
    assert m.r.devices["mouse"] == "no permission"


def test_reports_not_found(fake_evdev):
    m = FakeMission()
    asyncio.run(_run_for(m, 0.1))
    assert m.r.devices["mouse"] == "not found"


def test_busy_mouse_never_sends_stops(fake_evdev):
    """Another program holds the grab: report it, and don't stop the web pad's driving every retry."""
    fake_evdev.devs["/dev/input/event0"] = FakeMouse("/dev/input/event0", grab_error=errno.EBUSY)
    m = FakeMission()
    asyncio.run(_run_for(m, 0.1))
    assert m.r.devices["mouse"] == "busy"
    assert m.drives == []


def test_recorder_fault_keeps_driving(fake_evdev):
    class BadRecorder:
        def toggle(self, _t):
            raise RuntimeError("disk full")

    fake_evdev.devs["/dev/input/event0"] = FakeMouse("/dev/input/event0", [
        (0.01, EV_KEY, MID, 1), (0.01, EV_KEY, MID, 0), (0.05, EV_KEY, L, 1)])
    m = FakeMission(BadRecorder())
    asyncio.run(_run_for(m, 0.2))
    assert m.r.devices["mouse"] == "ok" and (1.0, 0.0) in m.drives
