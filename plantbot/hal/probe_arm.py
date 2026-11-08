"""Leaf-probe arm: a micro servo that swings the SensX25 pad onto a leaf.

position() / move_to() work in "arm units" 0..1 (0 = retracted, 1 = full press angle) so the
turgor routine doesn't care whether it's a servo, the rover itself, or the mock.

Drivers (config `tactile.arm.driver`):
  servo = SG90-class positional servo on a GPIO pin (gpiozero AngularServo)
  rover = no arm; the pad is on the nose and the rover creeps forward (turgor.probe handles it)
  mock  = moves MockTactile.indent_mm so mock frames respond like a real leaf
  none
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class ServoArm:
    kind = "servo"

    def __init__(self, cfg):
        from gpiozero import AngularServo  # lazy

        self.cfg = cfg
        self.retract_deg = cfg.get("retract_deg", 0)
        self.press_deg = cfg.get("press_deg", 60)
        self.s = AngularServo(cfg.pin, min_angle=-90, max_angle=90,
                              min_pulse_width=cfg.get("min_pulse_ms", 0.5) / 1000,
                              max_pulse_width=cfg.get("max_pulse_ms", 2.5) / 1000,
                              initial_angle=self.retract_deg)
        self.pos = 0.0

    def move_to(self, u: float) -> None:
        self.pos = min(1.0, max(0.0, u))
        self.s.angle = self.retract_deg + self.pos * (self.press_deg - self.retract_deg)

    def position(self) -> float:
        return self.pos

    def retract(self) -> None:
        self.move_to(0.0)

    def relax(self) -> None:
        """Stop sending pulses so the servo doesn't buzz while parked."""
        self.s.detach()

    def close(self) -> None:
        self.retract()
        self.s.close()


class MockArm:
    """pos 0..1 maps linearly onto travel_mm of pad travel; the leaf starts at gap_mm."""

    kind = "mock"

    def __init__(self, tactile, travel_mm: float = 8.0, gap_mm: float = 3.0):
        self.tactile, self.travel, self.gap = tactile, travel_mm, gap_mm
        self.pos = 0.0

    def move_to(self, u: float) -> None:
        self.pos = min(1.0, max(0.0, u))
        if hasattr(self.tactile, "indent_mm"):
            self.tactile.indent_mm = max(0.0, self.pos * self.travel - self.gap)

    def position(self) -> float:
        return self.pos

    def retract(self) -> None:
        self.move_to(0.0)

    def relax(self) -> None:
        pass

    def close(self) -> None:
        self.retract()


class NoArm:
    kind = "none"
    pos = 0.0

    def move_to(self, u: float) -> None:
        pass

    def position(self) -> float:
        return 0.0

    def retract(self) -> None:
        pass

    def relax(self) -> None:
        pass

    def close(self) -> None:
        pass


class RoverArm(NoArm):
    kind = "rover"


def make(cfg, tactile, mock: bool = False):
    """cfg = config `tactile.arm` (may be None). Returns (arm, status)."""
    driver = "mock" if mock else (cfg.get("driver", "none") if cfg is not None else "none")
    if driver == "mock":
        get = (lambda k, d: cfg.get(k, d)) if cfg is not None else (lambda k, d: d)
        return MockArm(tactile, get("mock_travel_mm", 8.0), get("mock_gap_mm", 3.0)), "mock"
    if driver == "rover":
        return RoverArm(), "rover"
    if driver in (None, "none"):
        return NoArm(), "none"
    try:
        return ServoArm(cfg), "ok"
    except Exception as e:
        log.error("probe arm failed to init: %s", e)
        return NoArm(), "offline"
