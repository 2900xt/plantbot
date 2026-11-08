"""Forward range sensor (HC-SR04)."""
from __future__ import annotations

import logging
import time
from typing import Protocol

log = logging.getLogger(__name__)


class Ranger(Protocol):
    def distance_cm(self) -> float | None: ...  # None = no echo


class HcSr04:
    def __init__(self, cfg):
        from gpiozero import DistanceSensor  # lazy

        self.max_cm = cfg.max_cm
        # partial=True: without it, .distance blocks until the queue fills, forever if nothing echoes
        self.s = DistanceSensor(echo=cfg.echo_pin, trigger=cfg.trig_pin, max_distance=cfg.max_cm / 100,
                                partial=True)

    def distance_cm(self) -> float | None:
        d = self.s.distance
        if d is None:
            return None
        d *= 100
        return None if d <= 0 or d >= self.max_cm - 1 else d


class NoRanger:
    def distance_cm(self) -> float | None:
        return None


class MockRanger:
    """Virtual row of plants every `spacing` cm. Integrates MockMotors forward speed."""

    def __init__(self, motors, spacing_cm: float = 60, speed_cm_s: float = 30, max_cm: float = 200):
        self.motors, self.spacing, self.speed, self.max_cm = motors, spacing_cm, speed_cm_s, max_cm
        self.pos = 10.0  # start 10 cm along: first plant is 50 cm ahead
        self.t = time.monotonic()

    def distance_cm(self) -> float | None:
        now = time.monotonic()
        self.pos += (self.motors.left + self.motors.right) / 2 * self.speed * (now - self.t)
        self.t = now
        ahead = self.spacing - (self.pos % self.spacing)  # distance to the next plant
        return ahead if ahead < self.max_cm else None


def make(cfg, mock_cfg, motors):
    if cfg.driver == "mock":
        return MockRanger(motors, mock_cfg.plant_spacing_cm, mock_cfg.speed_cm_s, cfg.max_cm), "mock"
    if cfg.driver in (None, "none"):
        return NoRanger(), "none"
    try:
        return HcSr04(cfg), "ok"
    except Exception as e:
        log.error("ranger failed to init: %s", e)
        return NoRanger(), "offline"
