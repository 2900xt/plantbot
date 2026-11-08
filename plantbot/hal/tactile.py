"""TouchTronix SensX25: 5 x 5 thin-film tactile pressure array over USB serial.

Protocol (SensX25 Technical Guide + vendor sensx25.py, event shared drive, fetched 2026-11-07):
- FT232 USB-UART adapter, shows up as /dev/ttyUSB0 on the Pi. 921600 baud, 8N1, no flow control.
- Streams from power-up, ~100 Hz, no start command. Each frame is 53 bytes:
  FF FF | 25 x uint16 big-endian (p0..p24, row-major as sent) | CRC-8/MAXIM over the 50 payload bytes.
- On a CRC mismatch, drop one byte and resync on the next FF FF.
- The grid is sent a quarter turn counterclockwise; np.rot90(g, -1) makes it upright
  (sensing face toward you, connector at the bottom, row 0 = top edge).
- Pressure mode (factory default): 0..534 per pixel, arbitrary units. ADC mode: 0..3945.
  Mode lives in Modbus register 0x6005 (0 = ADC, 1 = pressure), address 1, same line, and
  persists across power cycles: put it back to pressure before returning the kit.
- Each pixel reads 0 until a per-pixel activation threshold, so light contact reads 0.
  Pixels saturate individually, so `total` underestimates heavy loads.

Drivers (config `tactile.driver`):
  sensx25 = built-in reader (this file)
  vendor  = the vendor's own sensx25.py `frames()` generator, if you'd rather trust theirs
            (drop sensx25.py next to the repo or set tactile.vendor_path)
  mock    = synthetic leaf/bump physics, driven by MockProbeArm or poke()
  none    = disabled
Every driver runs a background reader thread and hands out TactileFrame objects.
"""
from __future__ import annotations

import importlib.util
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Protocol

import numpy as np

log = logging.getLogger(__name__)

BAUD = 921600
HEADER = b"\xff\xff"
FRAME_SIZE = 53
ROWS = COLS = 5
FULL_SCALE = {"pressure": 534, "adc": 3945}
MODE_REGISTER = 0x6005
MODES = {"adc": 0, "pressure": 1}
_ROWS_IDX, _COLS_IDX = np.indices((ROWS, COLS))


# ---------------------------------------------------------------- protocol
def crc8_maxim(data: bytes) -> int:
    """CRC-8/MAXIM (Dallas 1-Wire): poly 0x31 reflected (0x8C), init 0. check("123456789") = 0xA1."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


def crc16_modbus(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "little")


def modbus_request(func: int, value: int) -> bytes:
    """Modbus RTU to device 1, register 0x6005. func 0x06 writes `value`, 0x03 reads `value` registers."""
    body = bytes([1, func]) + MODE_REGISTER.to_bytes(2, "big") + value.to_bytes(2, "big")
    return body + crc16_modbus(body)


def encode_frame(grid_as_sent: np.ndarray) -> bytes:
    """Build a wire frame from a 5x5 grid in *sent* orientation (tests + mock serial)."""
    payload = np.asarray(grid_as_sent, dtype=">u2").reshape(25).tobytes()
    return HEADER + payload + bytes([crc8_maxim(payload)])


def upright_to_sent(grid: np.ndarray) -> np.ndarray:
    return np.rot90(np.asarray(grid), 1)


class FrameParser:
    """Incremental, CRC-checked parser. feed(bytes) -> list of upright 5x5 int grids."""

    def __init__(self, upright: bool = True):
        self.buf = bytearray()
        self.upright = upright
        self.good = 0
        self.bad_crc = 0

    def feed(self, data: bytes) -> list[np.ndarray]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(HEADER)
            if i < 0:
                del self.buf[:-1]  # keep a trailing FF: it may be half a header
                break
            if i:
                del self.buf[:i]
            if len(self.buf) < FRAME_SIZE:
                break
            payload = bytes(self.buf[2:52])
            if crc8_maxim(payload) != self.buf[52]:
                self.bad_crc += 1
                del self.buf[:1]  # not a real frame start: slide one byte and resync
                continue
            del self.buf[:FRAME_SIZE]
            g = np.frombuffer(payload, ">u2").reshape(ROWS, COLS).astype(np.int32)
            out.append(np.rot90(g, -1).copy() if self.upright else g)
            self.good += 1
        if len(self.buf) > 4096:  # garbage with no header: don't grow forever
            del self.buf[:-FRAME_SIZE]
        return out


# ---------------------------------------------------------------- frames
@dataclass
class TactileFrame:
    t: float                    # time.time()
    grid: np.ndarray            # 5x5, upright, baseline-subtracted, >= 0
    seq: int = 0
    total: float = field(init=False)
    peak: float = field(init=False)
    n_on: int = field(init=False)
    area: float = field(init=False)     # effective contact area in pixels (participation ratio)
    cop: tuple[float, float] | None = field(init=False)  # (row, col), sub-pixel

    def __post_init__(self):
        g = self.grid.astype(float)
        self.total = float(g.sum())
        self.peak = float(g.max())
        self.n_on = int((g > 0).sum())
        sq = float((g * g).sum())
        self.area = self.total * self.total / sq if sq > 0 else 0.0
        self.cop = ((float((g * _ROWS_IDX).sum() / self.total), float((g * _COLS_IDX).sum() / self.total))
                    if self.total > 0 else None)

    def summary(self) -> dict:
        return {"t": self.t, "total": round(self.total, 1), "peak": round(self.peak, 1), "n_on": self.n_on,
                "area": round(self.area, 2), "cop": None if self.cop is None else [round(c, 2) for c in self.cop]}


class Tactile(Protocol):
    def latest(self) -> TactileFrame | None: ...
    def history(self, seconds: float) -> list[TactileFrame]: ...
    def subscribe(self, fn: Callable[[TactileFrame], None]) -> None: ...
    def calibrate_zero(self, seconds: float = 1.0) -> np.ndarray: ...
    def close(self) -> None: ...


class _Base:
    """Shared plumbing: ring buffer, subscribers, zero-offset baseline, rate meter, reader thread."""

    name = "tactile"

    def __init__(self, cfg=None):
        get = (lambda k, d: cfg.get(k, d)) if cfg is not None else (lambda k, d: d)
        self.buf: deque[TactileFrame] = deque(maxlen=int(get("buffer_frames", 1000)))  # ~10 s at 100 Hz
        self.noise_floor = float(get("noise_floor", 0))        # per-pixel counts zeroed after baseline
        self.mode = get("mode", "pressure")
        self.zero = np.zeros((ROWS, COLS))
        self.lock = threading.Lock()
        self.subs: list[Callable[[TactileFrame], None]] = []
        self.seq = 0
        self.frames_total = 0
        self.last_rx = 0.0
        self._rate_t: deque[float] = deque(maxlen=200)
        self._running = False
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    # -- consumer API
    def latest(self) -> TactileFrame | None:
        with self.lock:
            return self.buf[-1] if self.buf else None

    def history(self, seconds: float) -> list[TactileFrame]:
        cutoff = time.time() - seconds
        with self.lock:
            return [f for f in self.buf if f.t >= cutoff]

    def subscribe(self, fn: Callable[[TactileFrame], None]) -> None:
        """fn runs on the reader thread for every frame (~100 Hz): keep it short, never block."""
        self.subs.append(fn)

    def rate_hz(self) -> float:
        with self.lock:
            ts = list(self._rate_t)
        return (len(ts) - 1) / (ts[-1] - ts[0]) if len(ts) > 2 and ts[-1] > ts[0] else 0.0

    def alive(self) -> bool:
        return time.time() - self.last_rx < 0.5

    def calibrate_zero(self, seconds: float = 1.0) -> np.ndarray:
        """Per-pixel median of the last `seconds` of raw frames, with nothing touching the pad.
        Pressure mode is 0 at rest already; this mostly matters in ADC mode or with a pre-loaded
        foam pad that presses on the film a little."""
        time.sleep(seconds)
        raws = [f.raw for f in self.history(seconds) if hasattr(f, "raw")]
        if raws:
            self.zero = np.median(np.stack(raws), axis=0)
        log.info("tactile zero offset: max %.1f mean %.1f", self.zero.max(), self.zero.mean())
        return self.zero

    def status(self) -> dict:
        f = self.latest()
        return {"alive": self.alive(), "rate_hz": round(self.rate_hz(), 1), "frames": self.frames_total,
                "error": self.error, **(f.summary() if f else {})}

    # -- producer side
    def _push(self, raw: np.ndarray, t: float | None = None) -> TactileFrame:
        raw = np.asarray(raw)
        g = np.clip(raw - self.zero, 0, None)
        if self.noise_floor:
            g[g <= self.noise_floor] = 0
        t = time.time() if t is None else t
        self.seq += 1
        fr = TactileFrame(t, g, self.seq)
        fr.raw = raw  # type: ignore[attr-defined]
        with self.lock:
            self.buf.append(fr)
            self._rate_t.append(t)
            self.frames_total += 1
            self.last_rx = t
        for fn in list(self.subs):
            try:
                fn(fr)
            except Exception:
                log.exception("tactile subscriber failed")
        return fr

    def _start(self, target: Callable[[], None]) -> None:
        self._running = True
        self._thread = threading.Thread(target=target, name=f"{self.name}-reader", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)


# ---------------------------------------------------------------- real
class SensX25(_Base):
    name = "sensx25"

    def __init__(self, cfg):
        super().__init__(cfg)
        import serial  # lazy: pyserial

        self.port = cfg.get("port", "/dev/ttyUSB0")
        self.ser = serial.Serial(self.port, cfg.get("baud", BAUD), timeout=0.05)
        self.parser = FrameParser()
        self._tail = bytearray()  # last raw bytes, so set_mode() can spot the Modbus reply
        self._start(self._loop)

    def _loop(self) -> None:
        while self._running:
            try:
                data = self.ser.read(self.ser.in_waiting or 1)
            except Exception as e:  # unplugged
                self.error = str(e)
                log.error("SensX25 read failed: %s", e)
                time.sleep(0.5)
                continue
            if data:
                self._tail += data
                del self._tail[:-512]
                for g in self.parser.feed(data):
                    self._push(g)

    def set_mode(self, mode: str) -> bool:
        """Switch "pressure"/"adc" over Modbus while streaming. Persists across power cycles."""
        value = MODES[mode]
        reply = bytes([1, 0x03, 2]) + value.to_bytes(2, "big")
        reply += crc16_modbus(reply)
        self.ser.write(modbus_request(0x06, value))
        for _ in range(10):
            time.sleep(0.3)  # sensor ignores requests for ~0.1-0.2 s after a write
            self._tail.clear()
            self.ser.write(modbus_request(0x03, 1))
            time.sleep(0.3)
            if reply in bytes(self._tail):  # reply lands between frames; the parser skips it
                self.mode = mode
                return True
        return False

    def close(self) -> None:
        super().close()
        try:
            self.ser.close()
        except Exception:
            pass


class VendorSensX25(_Base):
    """Wraps the vendor's sensx25.frames(port) generator (already upright), unchanged."""

    name = "sensx25-vendor"

    def __init__(self, cfg):
        super().__init__(cfg)
        path = cfg.get("vendor_path") or "sensx25.py"
        spec = importlib.util.spec_from_file_location("sensx25_vendor", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"vendor script not found: {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.gen: Iterator = mod.frames(cfg.get("port", "/dev/ttyUSB0"))
        self._start(self._loop)

    def _loop(self) -> None:
        try:
            for _t, grid in self.gen:
                if not self._running:
                    break
                self._push(np.asarray(grid))
        except Exception as e:
            self.error = str(e)
            log.error("vendor SensX25 reader died: %s", e)


# ---------------------------------------------------------------- mock
class MockTactile(_Base):
    """~100 Hz synthetic frames from a tiny contact model.

    A leaf has a turgor in [0, 1]. The probe arm sets `indent_mm` (how far past first contact
    the pad is). A turgid leaf pushes back hard over a small patch; a wilted leaf is soft and
    drapes over the pad, so the same force spreads over more pixels. That is exactly the
    force-vs-area signature turgor.py looks for.
    """

    name = "mock-tactile"

    def __init__(self, cfg=None, hz: float = 100.0, seed: int | None = 0):
        super().__init__(cfg)
        self.hz = hz
        self.rng = np.random.default_rng(seed)
        self.indent_mm = 0.0
        self.leaf_turgor = 0.8
        self._poke: tuple[float, float, float] | None = None  # (t_end, peak, sigma)
        self.full_scale = FULL_SCALE["pressure"]
        self._start(self._loop)

    def set_leaf(self, turgor: float) -> None:
        self.leaf_turgor = min(1.0, max(0.0, turgor))

    def poke(self, peak: float = 300.0, duration_s: float = 0.6, sigma: float = 0.9) -> None:
        """Bump the nose (dashboard 'mock bump' or tests)."""
        self._poke = (time.time() + duration_s, peak, sigma)

    def model_grid(self, indent_mm: float, turgor: float) -> np.ndarray:
        if indent_mm <= 0:
            return np.zeros((ROWS, COLS))
        # stiffness: N-ish per mm^1.5 (arbitrary units, unverified physics, just has the right shape)
        k = 40 + 260 * turgor
        force = k * indent_mm ** 1.5
        # patch radius in pixels: grows with indentation, much faster on a limp leaf
        spread = 0.5 + indent_mm * (0.12 + 0.5 * (1 - turgor))
        d2 = (_ROWS_IDX - 2.0) ** 2 + (_COLS_IDX - 2.0) ** 2
        w = np.exp(-d2 / (2 * spread ** 2))
        return force * w / w.sum()

    def _frame(self) -> np.ndarray:
        g = self.model_grid(self.indent_mm, self.leaf_turgor)
        if self._poke:
            t_end, peak, sigma = self._poke
            if time.time() < t_end:
                d2 = (_ROWS_IDX - 2.0) ** 2 + (_COLS_IDX - 2.0) ** 2
                g = g + peak * np.exp(-d2 / (2 * sigma ** 2))
            else:
                self._poke = None
        if g.any():
            g = g * (1 + 0.03 * self.rng.standard_normal(g.shape))
        g = np.where(g < 4, 0, g)  # activation threshold: light touch reads 0
        return np.clip(np.round(g), 0, self.full_scale).astype(np.int32)

    def _loop(self) -> None:
        period = 1.0 / self.hz
        nxt = time.monotonic()
        while self._running:
            self._push(self._frame())
            nxt += period
            time.sleep(max(0.0, nxt - time.monotonic()))


class NoTactile(_Base):
    name = "none"

    def __init__(self, cfg=None):
        super().__init__(cfg)


def replay(frames: Iterable[tuple[float, np.ndarray]]) -> list[TactileFrame]:
    """(t, upright grid) pairs, e.g. from the vendor CSV -> TactileFrames. For offline analysis."""
    return [TactileFrame(float(t), np.asarray(g), i) for i, (t, g) in enumerate(frames)]


def load_vendor_csv(path: str) -> list[TactileFrame]:
    """Read `python sensx25.py PORT --csv press.csv` output (t, p0..p24, upright)."""
    import csv

    out = []
    with open(path) as f:
        for i, row in enumerate(csv.DictReader(f)):
            g = np.array([float(row[f"p{k}"]) for k in range(25)]).reshape(ROWS, COLS)
            out.append(TactileFrame(float(row["t"]), g, i))
    return out


def make(cfg):
    """Returns (device, status) like the other HAL makers. cfg = config `tactile` section (may be None)."""
    driver = cfg.get("driver", "none") if cfg is not None else "none"
    if driver == "mock":
        return MockTactile(cfg), "mock"
    if driver in (None, "none"):
        return NoTactile(cfg), "none"
    try:
        dev = VendorSensX25(cfg) if driver == "vendor" else SensX25(cfg)
        return dev, "ok"
    except Exception as e:
        log.error("tactile (%s) failed to init: %s", driver, e)
        return NoTactile(cfg), "offline"

