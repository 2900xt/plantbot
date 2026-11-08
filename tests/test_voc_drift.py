import math
import random

from plantbot.config import Cfg
from plantbot.sensing.voc_drift import DriftCal, DriftDetector

SIG = 0.02  # ln-units of sensor noise (~2% resistance)


def make(**kw):
    return DriftDetector(Cfg(**{"calib_s": 60, "rate_hz": 1, "hold_s": 5, **kw}), path="")


def feed(d, t0, n, mq2=lambda t: 0.0, bme=lambda t: 0.0, rh=lambda t: 45.0, rng=None, sensors=("mq2", "bme")):
    """Shifts are in ln units: + means more VOCs (resistance falls)."""
    rng = rng or random.Random(1)
    st = None
    for i in range(n):
        t = t0 + i
        rs = 10.0 * math.exp(-mq2(t) + rng.gauss(0, SIG)) if "mq2" in sensors else None
        gas = 120_000 * math.exp(-bme(t) + rng.gauss(0, SIG)) if "bme" in sensors else None
        st = d.update(t, rs, gas, rh(t), 22.0) or st
    return st


def calibrated(**kw):
    d = make(**kw)
    st = feed(d, 0, 62)
    assert st["status"] == "healthy", st
    return d


def test_calibration_learns_mean_and_sigma():
    d = calibrated()
    for name in ("mq2", "bme"):
        sc = d.cal.sensors[name]
        assert abs(sc.sigma_raw - SIG) < 0.008
    assert abs(d.cal.sensors["mq2"].mu - math.log(10.0)) < 0.02


def test_waits_for_warmup_then_calibrates():
    d = make()
    assert d.update(0, 10.0, 120_000, 45, 22, ready=False)["status"] == "waiting"
    st = d.update(1, 10.0, 120_000, 45, 22)
    assert st["status"] == "calibrating" and st["calib_progress"] == 0.0


def test_quiet_air_stays_healthy():
    d = calibrated()
    st = feed(d, 100, 600, rng=random.Random(7))
    assert st["status"] == "healthy"
    assert abs(st["z"]) < 1.5


def test_step_rise_on_both_sensors_is_diseased():
    d = calibrated()
    st = feed(d, 100, 3, mq2=lambda t: 5 * SIG, bme=lambda t: 5 * SIG)
    assert st["status"] == "watch"  # not yet held for hold_s
    st = feed(d, 103, 8, mq2=lambda t: 5 * SIG, bme=lambda t: 5 * SIG)
    assert st["status"] == "diseased"
    assert st["stress"] == 100


def test_one_sensor_rise_is_only_watch():
    d = calibrated()
    st = feed(d, 100, 30, mq2=lambda t: 5 * SIG)
    assert st["status"] == "watch"


def test_short_spike_does_not_alarm():
    d = calibrated()
    spike = lambda t: 6 * SIG if 100 <= t < 102 else 0.0
    for i in range(40):
        st = feed(d, 100 + i, 1, mq2=spike, bme=spike, rng=random.Random(i))
        assert st["status"] != "diseased", i
    assert st["status"] == "healthy"


def test_slow_drift_is_caught_by_cusum_before_3_sigma():
    d = calibrated()
    # 1.5 sigma, steady: never crosses z_alarm=3, but CUSUM adds ~1 per sample
    st = feed(d, 100, 30, mq2=lambda t: 1.5 * SIG, bme=lambda t: 1.5 * SIG)
    assert st["sensors"]["mq2"]["z_ewma"] < 3
    assert st["status"] == "diseased"
    d.reset_cusum()
    assert d.tracks["mq2"].cusum == 0


def test_humidity_compensation():
    d = make(min_rh_span=3.0)
    # During calibration RH swings 40-50 % and the BME680 reads 2 %/%RH lower when humid
    rh = lambda t: 45 + 5 * math.sin(t / 5)
    feed(d, 0, 62, bme=lambda t: 0.02 * (rh(t) - 45), rh=rh)
    assert -0.025 < d.cal.sensors["bme"].b_rh < -0.015  # ln R falls 2 % per %RH
    # Watering: RH jumps +10 %. Uncompensated that's +10 sigma on the BME680.
    st = feed(d, 100, 60, bme=lambda t: 0.02 * 10, rh=lambda t: 55)
    assert st["status"] == "healthy", st


def test_single_sensor_works_with_low_confidence():
    d = make()
    feed(d, 0, 62, sensors=("bme",))
    st = feed(d, 100, 20, bme=lambda t: 5 * SIG, sensors=("bme",))
    assert st["status"] == "diseased" and st["confidence"] == "low"


def test_calibration_saved_and_reloaded(tmp_path):
    p = str(tmp_path / "drift.json")
    d = DriftDetector(Cfg(calib_s=30), path=p)
    feed(d, 0, 32)
    assert DriftCal.load(p).sensors["mq2"].n >= 30
    d2 = DriftDetector(Cfg(calib_s=30), path=p)
    assert d2.status_ == "healthy" and d2.cal.sensors.keys() == d.cal.sensors.keys()


def test_no_sensors():
    d = make()
    assert d.update(0, None, None)["status"] == "no_data"


def test_alarm_clears_after_big_smell_passes():
    d = calibrated()
    st = feed(d, 100, 60, mq2=lambda t: 40 * SIG, bme=lambda t: 40 * SIG)
    assert st["status"] == "diseased" and st["cusum"] <= 16
    st = feed(d, 160, 45)
    assert st["status"] == "healthy"
