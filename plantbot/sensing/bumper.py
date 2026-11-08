"""Gentle-contact bumper: SensX25 on the rover nose stops the motors on contact.

ContactGuard subscribes to the tactile reader thread, so the stop happens within a frame or
two (~10-20 ms at 100 Hz) instead of waiting for the 10 Hz mission tick. It calls the
motors' stop() directly from the reader thread (SafeMotors.stop takes its own lock).

GuardedMotors wraps the motors object so a held drive key can't push on into the plant:
while contact is latched, forward motion is refused but reverse is allowed (back away).
Every contact is logged (in memory + CSV) with peak force, duration and center of pressure.
Callbacks run on the tactile reader thread: on_trip(event) at the moment of contact,
on_event(event) on release (with duration). Hop to the event loop with call_soon_threadsafe.
"""
from __future__ import annotations

import csv
import logging
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Callable

log = logging.getLogger(__name__)


class ContactGuard:
    def __init__(self, tactile, motors=None, cfg=None, csv_path: str | None = None,
                 on_event: Callable[[dict], None] | None = None, on_trip: Callable[[dict], None] | None = None):
        get = (lambda k, d: cfg.get(k, d)) if cfg is not None else (lambda k, d: d)
        self.enabled = bool(get("enabled", True))
        self.stop_force = float(get("stop_force", 60.0))   # a.u. total that counts as a bump (unverified)
        self.release_force = float(get("release_force", 20.0))
        self.debounce = int(get("debounce_frames", 2))
        self.hold_s = float(get("hold_s", 1.0))             # forward stays blocked this long after release
        self.only_when_moving = bool(get("only_when_moving", False))
        self.tactile, self.motors, self.on_event, self.on_trip = tactile, motors, on_event, on_trip
        self.csv_path = csv_path
        self.events: deque[dict] = deque(maxlen=100)
        self.trips = 0
        self.contact = False
        self.blocked_until = 0.0
        self._n = 0
        self._suspend = 0
        self._cur: dict | None = None
        self.lock = threading.Lock()
        if tactile is not None:
            tactile.subscribe(self._on_frame)

    # -- state
    @property
    def armed(self) -> bool:
        return self.enabled and self._suspend == 0

    def blocking(self) -> bool:
        """True while forward driving should be refused."""
        return self.armed and (self.contact or time.monotonic() < self.blocked_until)

    @contextmanager
    def suspended(self):
        """Disarm during a deliberate touch (turgor probe pushing the leaf)."""
        with self.lock:
            self._suspend += 1
        try:
            yield self
        finally:
            with self.lock:
                self._suspend -= 1
                self.contact, self._n, self._cur = False, 0, None

    def status(self) -> dict:
        last = self.events[-1] if self.events else None
        return {"armed": self.armed, "contact": self.contact, "blocking": self.blocking(),
                "trips": self.trips, "last": last}

    # -- reader-thread callback
    def _on_frame(self, f) -> None:
        if not self.armed:
            return
        if not self.contact:
            moving = getattr(self.motors, "moving", True)
            hit = f.total >= self.stop_force and (moving or not self.only_when_moving)
            self._n = self._n + 1 if hit else 0
            if self._n >= self.debounce:
                self._trip(f)
        else:
            c = self._cur
            if c is not None and f.total > c["peak_force"]:
                c["peak_force"], c["peak_pixel"], c["cop"] = round(f.total, 1), round(f.peak, 1), f.cop
            if f.total < self.release_force:
                self._release(f)

    def _trip(self, f) -> None:
        if self.motors is not None:
            was = (round(getattr(self.motors, "left", 0.0), 2), round(getattr(self.motors, "right", 0.0), 2))
            try:
                self.motors.stop()
            except Exception:
                log.exception("bumper stop failed")
        else:
            was = (0.0, 0.0)
        self.contact = True
        self.trips += 1
        self._cur = {"t": f.t, "peak_force": round(f.total, 1), "peak_pixel": round(f.peak, 1),
                     "cop": f.cop, "motors_before": list(was), "duration_ms": None}
        log.warning("BUMP: contact %.0f a.u. at cop=%s, motors stopped (were %s)", f.total, f.cop, was)
        if self.on_trip:
            try:
                self.on_trip(dict(self._cur))
            except Exception:
                log.exception("bumper on_trip failed")

    def _release(self, f) -> None:
        c = self._cur or {"t": f.t}
        c["duration_ms"] = round((f.t - c["t"]) * 1000)
        c["cop"] = None if c.get("cop") is None else [round(x, 2) for x in c["cop"]]
        self.contact, self._n, self._cur = False, 0, None
        self.blocked_until = time.monotonic() + self.hold_s
        self.events.append(c)
        self._write_csv(c)
        if self.on_event:
            try:
                self.on_event(c)
            except Exception:
                log.exception("bumper on_event failed")

    def _write_csv(self, e: dict) -> None:
        if not self.csv_path:
            return
        try:
            new = not os.path.exists(self.csv_path)
            os.makedirs(os.path.dirname(self.csv_path) or ".", exist_ok=True)
            with open(self.csv_path, "a", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(["t", "peak_force", "peak_pixel", "duration_ms", "cop_row", "cop_col",
                                "motor_l", "motor_r"])
                cop = e.get("cop") or [None, None]
                w.writerow([round(e["t"], 3), e["peak_force"], e["peak_pixel"], e["duration_ms"], cop[0], cop[1],
                            *e.get("motors_before", [None, None])])
        except Exception:
            log.exception("contact csv write failed")


class GuardedMotors:
    """Drop-in wrapper for SafeMotors: refuses forward drive while the bumper is blocking.
    Everything else (left, right, watchdog_trips, moving, close, ...) passes through."""

    def __init__(self, inner, guard: ContactGuard):
        self.inner, self.guard = inner, guard
        guard.motors = inner
        self.refused = 0

    def drive(self, left: float, right: float) -> None:
        if self.guard.blocking() and (left > 0 or right > 0):
            # allow pure reverse/turn-away: clip the forward components to 0
            left, right = min(left, 0.0), min(right, 0.0)
            self.refused += 1
            if left == 0 and right == 0:
                self.inner.stop()
                return
        self.inner.drive(left, right)

    def __getattr__(self, name):
        return getattr(self.inner, name)
