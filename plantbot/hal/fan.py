"""Sensor-pod fan on a logic-level MOSFET."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class GpioFan:
    def __init__(self, cfg):
        from gpiozero import OutputDevice  # lazy

        self.dev = OutputDevice(cfg.pin, initial_value=False)

    def on(self) -> None:
        self.dev.on()

    def off(self) -> None:
        self.dev.off()


class MockFan:
    def __init__(self):
        self.is_on = False

    def on(self) -> None:
        self.is_on = True

    def off(self) -> None:
        self.is_on = False


def make(cfg):
    if cfg.driver == "mock":
        return MockFan(), "mock"
    if cfg.driver in (None, "none"):
        return MockFan(), "none"
    try:
        return GpioFan(cfg), "ok"
    except Exception as e:
        log.error("fan failed to init: %s", e)
        return MockFan(), "offline"
