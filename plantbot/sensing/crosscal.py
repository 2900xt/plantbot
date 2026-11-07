"""Put the MQ-2 on the BME680's scale so the two can be subtracted.

The two sensors' raw resistances can't be converted into each other (different oxide,
heater temperature and selectivity). What we can do:

1. Express each as a log ratio against its own clean-air baseline:
   lr = ln(R_baseline / R_now), so 0 = clean air and > 0 = more VOCs.
2. Co-locate both sensors, expose them to the same smells, and fit
   lr_bme ≈ a * lr_mq2 + b   (tools/crosscal.py does this and writes the JSON).
3. Differential = plant-side reading minus background reading, both in BME680 units.

Log ratios are baseline-independent, so "Recalibrate" (new baselines) doesn't
invalidate the fit. A new fit is needed if a sensor or its heater settings change.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)


def log_ratio(base: float | None, now: float | None) -> float | None:
    """ln(base / now). Both are resistances; resistance drops when VOCs rise."""
    if not base or not now or base <= 0 or now <= 0 or math.isnan(base) or math.isnan(now):
        return None
    return math.log(base / now)


@dataclass
class CrossCal:
    a: float = 1.0  # BME680 log-ratio per MQ-2 log-ratio
    b: float = 0.0
    r2: float | None = None
    n: int = 0
    x_span: float | None = None  # range of MQ-2 log ratios seen during the fit
    t: float | None = None
    calibrated: bool = False  # False = identity map, a placeholder until crosscal.py runs

    def mq2_to_bme(self, lr_mq2: float | None) -> float | None:
        return None if lr_mq2 is None else self.a * lr_mq2 + self.b

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: str | None) -> "CrossCal":
        if not path or not os.path.exists(path):
            log.warning("no MQ-2/BME680 cross-calibration at %s; differential uses an identity map", path)
            return cls()
        with open(path) as f:
            return cls(**json.load(f))


def fit(x_mq2: list[float], y_bme: list[float]) -> CrossCal:
    """Least squares y = a x + b."""
    n = len(x_mq2)
    if n < 3:
        raise ValueError(f"need at least 3 paired samples, got {n}")
    mx, my = sum(x_mq2) / n, sum(y_bme) / n
    sxx = sum((x - mx) ** 2 for x in x_mq2)
    if sxx <= 0:
        raise ValueError("MQ-2 never moved during calibration, so there is nothing to fit")
    sxy = sum((x - mx) * (y - my) for x, y in zip(x_mq2, y_bme))
    a = sxy / sxx
    b = my - a * mx
    ss_res = sum((y - (a * x + b)) ** 2 for x, y in zip(x_mq2, y_bme))
    ss_tot = sum((y - my) ** 2 for y in y_bme)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None
    return CrossCal(a=a, b=b, r2=r2, n=n, x_span=max(x_mq2) - min(x_mq2), t=time.time(), calibrated=True)


def differential(lr_mq2: float | None, lr_bme: float | None, cal: CrossCal, plant_sensor: str) -> float | None:
    """Plant-side minus background, in BME680 log-ratio units. > 0 = plant side smells more."""
    mq2_on_bme = cal.mq2_to_bme(lr_mq2)
    if mq2_on_bme is None or lr_bme is None:
        return None
    return mq2_on_bme - lr_bme if plant_sensor == "mq2" else lr_bme - mq2_on_bme
