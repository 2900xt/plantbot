"""VOC + climate sensors: BME680 (I2C) and MQ-2 via ADS1115 (I2C ADC)."""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import asdict, dataclass
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass
class EnvReading:
    t: float
    mq2_v: float  # ADS1115 volts at the MQ-2 AO (after divider, scaled back)
    gas_ohms: float | None  # BME680 gas resistance; None until heater has settled
    temp_c: float
    rh: float
    pressure_hpa: float

    def to_dict(self) -> dict:
        return asdict(self)


class EnvSensors(Protocol):
    def read(self) -> EnvReading: ...


def mq2_rs_kohm(v_out: float, vc: float, rl_kohm: float) -> float:
    """Sensor resistance from the module's AO voltage: rs = RL * (Vc - Vout) / Vout."""
    v_out = max(v_out, 1e-3)
    return rl_kohm * (vc - v_out) / v_out


class RealEnv:
    """BME680 + ADS1115. Either can be missing; missing values come back as NaN/None.

    The BME680 gas measurement takes ~150 ms of heater time, so it's read in its own
    thread at ~1 Hz and read() returns the latest cached value.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.status = {}
        self.ads = self.bme = None
        try:
            import board  # lazy: adafruit-blinka
            import busio
            from adafruit_ads1x15 import ads1115 as ADS
            from adafruit_ads1x15.analog_in import AnalogIn

            i2c = busio.I2C(board.SCL, board.SDA)
            ads = ADS.ADS1115(i2c, address=cfg.ads1115_address, gain=cfg.ads1115_gain)
            self.ads = AnalogIn(ads, cfg.ads1115_channel)  # v6 API: channel index
            self.status["mq2"] = "ok"
        except Exception as e:
            log.error("ADS1115/MQ-2 init failed: %s", e)
            self.status["mq2"] = "offline"
        try:
            import bme680  # lazy: pimoroni bme680 lib

            b = bme680.BME680(cfg.bme680_address)
            b.set_humidity_oversample(bme680.OS_2X)
            b.set_pressure_oversample(bme680.OS_4X)
            b.set_temperature_oversample(bme680.OS_8X)
            b.set_filter(bme680.FILTER_SIZE_3)
            b.set_gas_status(bme680.ENABLE_GAS_MEAS)
            b.set_gas_heater_temperature(320)
            b.set_gas_heater_duration(150)
            b.select_gas_heater_profile(0)
            self.bme = b
            self.status["bme680"] = "ok"
        except Exception as e:
            log.error("BME680 init failed: %s", e)
            self.status["bme680"] = "offline"
        if not self.ads and not self.bme:
            raise RuntimeError("no env sensors")
        self._bme_cache = (None, math.nan, math.nan, math.nan)
        if self.bme:
            threading.Thread(target=self._bme_loop, name="bme680", daemon=True).start()

    def _bme_loop(self) -> None:
        while True:
            try:
                if self.bme.get_sensor_data():
                    d = self.bme.data
                    gas = d.gas_resistance if d.heat_stable else None
                    self._bme_cache = (gas, d.temperature, d.humidity, d.pressure)
            except Exception as e:
                log.warning("BME680 read failed: %s", e)
            time.sleep(1.0)

    def read(self) -> EnvReading:
        mq2 = math.nan
        if self.ads:
            try:
                mq2 = self.ads.voltage * self.cfg.mq2.divider
            except Exception as e:
                log.warning("ADS1115 read failed: %s", e)
        gas, t, rh, p = self._bme_cache
        return EnvReading(time.time(), mq2, gas, t, rh, p)


class MockEnv:
    """Fake sensors with drift, noise, a warm-up curve, and scriptable VOC events.

    Script entries: {t, duration, mq2_dv, gas_scale}, t in seconds after start.
    spike() adds an event starting now (dashboard "Mock VOC spike" button).
    """

    def __init__(self, script=None, spike_cfg=None):
        self.t0 = time.time()
        self.events = [dict(vars(e)) if hasattr(e, "__dict__") else dict(e) for e in (script or [])]
        self.spike_cfg = dict(vars(spike_cfg)) if spike_cfg else {"duration": 10, "mq2_dv": 0.4, "gas_scale": 0.55}

    def spike(self, **kw) -> None:
        ev = {**self.spike_cfg, **kw, "t": time.time() - self.t0}
        self.events.append(ev)
        log.info("mock VOC spike: %s", ev)

    def read(self) -> EnvReading:
        now = time.time()
        el = now - self.t0
        warm = math.exp(-el / 1.0)  # MQ-2 reads high while the heater warms up
        mq2 = 0.55 + 0.6 * warm + 0.02 * math.sin(el / 20) + random.gauss(0, 0.004)
        gas = 120_000 * (1 + 0.03 * math.sin(el / 30)) * random.gauss(1, 0.004)
        for ev in self.events:
            dt = el - ev["t"]
            if 0 <= dt <= ev["duration"]:
                # smooth rise and fall over the event
                k = math.sin(math.pi * dt / ev["duration"]) ** 0.5
                mq2 += ev.get("mq2_dv", 0) * k
                gas *= 1 - (1 - ev.get("gas_scale", 1)) * k
        return EnvReading(now, mq2, gas if el > 2 else None, 22.5 + random.gauss(0, 0.05),
                          45 + random.gauss(0, 0.2), 1012 + random.gauss(0, 0.1))


class NoEnv:
    """No VOC/climate sensors fitted: every value is missing."""

    def read(self) -> EnvReading:
        nan = float("nan")
        return EnvReading(time.time(), nan, None, nan, nan, nan)


def make(cfg, mock_cfg):
    """Returns (env, status dict)."""
    if cfg.driver in (None, "none"):
        return NoEnv(), {"mq2": "none", "bme680": "none"}
    if cfg.driver == "mock":
        return MockEnv(mock_cfg.env_script, mock_cfg.spike), {"mq2": "mock", "bme680": "mock"}
    try:
        env = RealEnv(cfg)
        return env, env.status
    except Exception as e:
        log.error("env sensors failed to init: %s; using mock", e)
        return MockEnv(), {"mq2": "offline", "bme680": "offline"}
