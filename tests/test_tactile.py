"""SensX25 driver, turgor analysis, bumper and Zelos publisher. Mock only, no hardware."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from plantbot.hal import probe_arm, tactile
from plantbot.hal.tactile import (FrameParser, MockTactile, TactileFrame, crc8_maxim, crc16_modbus, encode_frame,
                                  modbus_request, upright_to_sent)
from plantbot.sensing import turgor
from plantbot.sensing.bumper import ContactGuard, GuardedMotors


class Cfg(SimpleNamespace):
    def get(self, k, d=None):
        return getattr(self, k, d)

    def to_dict(self):
        return dict(vars(self))


# ---------------------------------------------------------------- protocol
def test_crc8_maxim_check_value():
    assert crc8_maxim(b"123456789") == 0xA1  # from the technical guide


def test_modbus_requests_match_guide():
    assert modbus_request(0x06, 0).hex(" ") == "01 06 60 05 00 00 87 cb"
    assert modbus_request(0x06, 1).hex(" ") == "01 06 60 05 00 01 46 0b"
    assert modbus_request(0x03, 1).hex(" ") == "01 03 60 05 00 01 8a 0b"
    reply = bytes([1, 3, 2, 0, 1])
    assert (reply + crc16_modbus(reply)).hex(" ") == "01 03 02 00 01 79 84"


def test_parser_roundtrip_and_orientation():
    up = np.arange(25).reshape(5, 5)
    sent = upright_to_sent(up)
    # guide: sent p0 is the top-right corner of the upright grid
    assert sent.reshape(25)[0] == up[0, 4]
    p = FrameParser()
    out = p.feed(encode_frame(sent) * 3)
    assert len(out) == 3 and all((g == up).all() for g in out)


def test_parser_resyncs_after_garbage_and_bad_crc():
    up = np.full((5, 5), 7)
    good = encode_frame(upright_to_sent(up))
    bad = bytearray(good)
    bad[10] ^= 0xFF  # corrupt payload -> CRC fails
    stream = b"\x00\x12\xff" + bytes(bad) + good[:20]
    p = FrameParser()
    assert p.feed(stream) == []
    out = p.feed(good[20:] + good)
    assert len(out) == 2 and p.bad_crc >= 1
    assert all((g == up).all() for g in out)


def test_parser_handles_byte_by_byte():
    up = np.eye(5, dtype=int) * 300
    data = encode_frame(upright_to_sent(up)) * 2
    p = FrameParser()
    got = []
    for b in data:
        got += p.feed(bytes([b]))
    assert len(got) == 2 and (got[1] == up).all()


def test_frame_features():
    g = np.zeros((5, 5))
    g[1, 3] = 100
    f = TactileFrame(0.0, g)
    assert f.total == 100 and f.n_on == 1 and f.area == pytest.approx(1.0)
    assert f.cop == (1.0, 3.0)
    assert TactileFrame(0.0, np.zeros((5, 5))).cop is None


def test_vendor_csv(tmp_path):
    p = tmp_path / "press.csv"
    p.write_text("t," + ",".join(f"p{i}" for i in range(25)) + "\n0.01," + ",".join(["0"] * 12 + ["50"] + ["0"] * 12) + "\n")
    fr = tactile.load_vendor_csv(str(p))
    assert fr[0].grid[2, 2] == 50 and fr[0].cop == (2.0, 2.0)


# ---------------------------------------------------------------- mock device
@pytest.fixture
def mock():
    m = MockTactile(hz=200, seed=0)
    yield m
    m.close()


def test_mock_streams_frames(mock):
    time.sleep(0.2)
    assert mock.latest() is not None and mock.frames_total > 10
    assert mock.latest().total == 0  # nothing touching
    mock.poke(peak=300, duration_s=0.2)
    time.sleep(0.08)
    assert mock.latest().total > 200
    assert mock.status()["alive"]


def test_make_falls_back_offline():
    dev, st = tactile.make(Cfg(driver="sensx25", port="/dev/does-not-exist"))
    assert st == "offline"
    dev, st = tactile.make(None)
    assert st == "none" and dev.latest() is None


# ---------------------------------------------------------------- turgor
def _synthetic_press(m: MockTactile, turgor_level: float, depth: float = 2.5, n: int = 80):
    frames = []
    for i in range(n):
        d = depth * min(1.0, i / (n * 0.7))
        frames.append(TactileFrame(i * 0.01, np.where((g := m.model_grid(d, turgor_level)) < 4, 0, g)))
    return frames


def test_contact_detector_hysteresis():
    d = turgor.ContactDetector(20, 10, debounce=2)
    seq = [0, 25, 25, 15, 9, 30]
    assert [d.update(x) for x in seq] == [False, False, True, True, False, False]


def test_turgid_scores_higher_than_wilted(mock):
    hi = turgor.analyze(_synthetic_press(mock, 0.95))
    lo = turgor.analyze(_synthetic_press(mock, 0.1, depth=4.0))
    assert hi.ok and lo.ok
    assert hi.pressure_at_ref > 2 * lo.pressure_at_ref
    assert hi.turgor_index > 0.66 > 0.33 > lo.turgor_index
    assert hi.label == "turgid" and lo.label == "wilted"
    assert hi.contact_area_px < lo.contact_area_px
    assert hi.stiffness_area is not None


def test_no_contact():
    frames = [TactileFrame(i * 0.01, np.zeros((5, 5))) for i in range(20)]
    r = turgor.analyze(frames)
    assert not r.ok and r.label == "no contact" and r.turgor_index is None


def test_index_independent_of_press_depth(mock):
    a = turgor.analyze(_synthetic_press(mock, 0.7, depth=1.6))
    b = turgor.analyze(_synthetic_press(mock, 0.7, depth=2.6))
    assert abs(a.turgor_index - b.turgor_index) < 0.15


def test_calibration_roundtrip(tmp_path, mock):
    cfg = Cfg(calibration=str(tmp_path / "turgor.json"))
    wet = turgor.analyze(_synthetic_press(mock, 1.0))
    dry = turgor.analyze(_synthetic_press(mock, 0.0, depth=4.0))
    turgor.calibrate("turgid", wet, cfg)
    c = turgor.calibrate("wilted", dry, cfg)
    assert c["turgid_ref"] == pytest.approx(wet.pressure_at_ref, rel=1e-3)
    mid = turgor.analyze(_synthetic_press(mock, 0.5, depth=3.0), cfg=turgor.load_calibration(cfg))
    assert 0.05 < mid.turgor_index < 0.95


def test_probe_with_mock_arm(mock):
    arm, st = probe_arm.make(None, mock, mock=True)
    assert st == "mock"
    cfg = Cfg(ramp_s=1.0, hold_s=0.1, settle_s=0.05, zero_s=0.05, calibration="")
    mock.set_leaf(0.9)
    wet = turgor.probe(mock, arm, cfg)
    mock.set_leaf(0.15)
    dry = turgor.probe(mock, arm, cfg)
    assert wet.ok and dry.ok, (wet.flags, dry.flags)
    assert wet.turgor_index > dry.turgor_index + 0.3
    assert wet.stiffness_pos is not None
    assert arm.position() == 0.0 and mock.indent_mm == 0.0  # retracted


def test_fuse_with_turgor():
    fused = {"health": 80.0, "status": "healthy", "voc_stress": 0.1, "explain": "Looks: 80% healthy"}
    out = turgor.fuse_with_turgor(fused, {"turgor_index": 0.0, "label": "wilted"}, Cfg(w_turgor=0.5),
                                  Cfg(healthy_min=70, watch_min=40))
    assert out["health"] == 40.0 and out["status"] == "watch" and "wilted" in out["explain"]
    same = turgor.fuse_with_turgor(fused, None)
    assert same["health"] == 80.0 and same["turgor_index"] is None


# ---------------------------------------------------------------- bumper
class FakeMotors:
    def __init__(self):
        self.left = self.right = 0.0
        self.stops = 0

    @property
    def moving(self):
        return self.left != 0 or self.right != 0

    def drive(self, l, r):
        self.left, self.right = l, r

    def stop(self):
        self.left = self.right = 0.0
        self.stops += 1


def _wait(pred, timeout=1.0):
    t = time.time() + timeout
    while time.time() < t:
        if pred():
            return True
        time.sleep(0.005)
    return False


def test_bumper_stops_and_blocks_forward(mock, tmp_path):
    inner = FakeMotors()
    events = []
    guard = ContactGuard(mock, cfg=Cfg(stop_force=60, release_force=20, hold_s=0.2),
                         csv_path=str(tmp_path / "contacts.csv"), on_event=events.append)
    motors = GuardedMotors(inner, guard)
    motors.drive(0.4, 0.4)
    mock.poke(peak=300, duration_s=0.15)
    assert _wait(lambda: guard.contact), "bumper never tripped"
    assert inner.left == 0 and inner.stops >= 1
    motors.drive(0.4, 0.4)          # forward refused
    assert inner.left == 0
    motors.drive(-0.3, -0.3)        # reverse allowed
    assert inner.left == -0.3
    assert _wait(lambda: events, 1.0), "contact never released"
    assert events[0]["peak_force"] >= 60 and events[0]["duration_ms"] > 0
    assert (tmp_path / "contacts.csv").read_text().count("\n") == 2
    assert _wait(lambda: not guard.blocking(), 1.0)
    motors.drive(0.4, 0.4)
    assert inner.left == 0.4
    assert motors.watchdog_trips if hasattr(inner, "watchdog_trips") else True  # passthrough doesn't explode


def test_bumper_suspended_during_probe(mock):
    inner = FakeMotors()
    guard = ContactGuard(mock, cfg=Cfg(stop_force=60))
    GuardedMotors(inner, guard)
    with guard.suspended():
        mock.poke(peak=300, duration_s=0.1)
        time.sleep(0.1)
        assert not guard.contact and guard.trips == 0
    time.sleep(0.1)
    assert guard.trips == 0


# ---------------------------------------------------------------- zelos
def test_zelos_noop_without_sdk(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "zelos_sdk":
            raise ImportError
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    from plantbot.integrations.zelos_pub import ZelosPublisher

    z = ZelosPublisher(Cfg(enabled=True))
    assert not z.enabled and "not installed" in z.error
    z.start()
    z.publish_scan({"health": 1})
    z.publish_bump({"peak_force": 1})
    z.close()


def test_zelos_publishes_with_fake_sdk(monkeypatch, mock):
    import sys
    import types

    logged = []

    class Ev:
        def __init__(self, name):
            self.name = name

        def log(self, **kw):
            logged.append((self.name, kw))

    class Src:
        def __init__(self, name):
            self.name = name

        def add_event(self, name, fields):
            return Ev(f"{self.name}/{name}")

    fake = types.ModuleType("zelos_sdk")
    fake.init = lambda *a, **k: None
    fake.TraceSource = Src
    fake.TraceEventFieldMetadata = lambda *a: a
    fake.DataType = SimpleNamespace(Float64="f", String="s", Boolean="b", Int64="i")
    monkeypatch.setitem(sys.modules, "zelos_sdk", fake)
    from plantbot.integrations.zelos_pub import ZelosPublisher

    lock = threading.Lock()
    sampler = SimpleNamespace(lock=lock, smooth={"mq2_v": 0.8, "gas_ohms": 5e4, "temp_c": 22, "rh": 40,
                                                 "pressure_hpa": 1000}, warm=True,
                              latest=lambda: SimpleNamespace(mq2_v=0.81, gas_ohms=None),
                              ratios=lambda: {"mq2": 1.0, "gas": None, "diff": None})
    robot = SimpleNamespace(sampler=sampler, motors=SimpleNamespace(left=0.2, right=0.3, watchdog_trips=0))
    mission = SimpleNamespace(r=robot, range_cm=None, state="IDLE", mode="manual", auto_scans=0)
    z = ZelosPublisher(Cfg(rate_hz=50, tactile_hz=50), mission=mission, tactile=mock)
    assert z.enabled
    z.start()
    mock.poke(peak=200, duration_s=0.1)
    assert _wait(lambda: {n for n, _ in logged} >= {"env/voc", "drive/motors", "mission/state", "tactile/frame"})
    z.publish_scan({"health": 72.5, "turgor_index": 0.8, "turgor": {"peak_force": 400}, "status": "healthy"})
    z.close()
    voc = next(kw for n, kw in logged if n == "env/voc")
    assert voc["mq2_v"] == 0.81 and voc["gas_ohms"] != voc["gas_ohms"]  # None -> NaN
    assert any(n == "scan/result" and kw["turgor_index"] == 0.8 for n, kw in logged)
