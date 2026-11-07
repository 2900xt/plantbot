#!/usr/bin/env python3
"""Fit the MQ-2 -> BME680 mapping used by the plant-vs-background VOC differential.

Put the MQ-2 and BME680 side by side (same air, same distance from the source), warmed up.

    python tools/crosscal.py --seconds 180

1. First --baseline-s seconds: clean air, nothing nearby. Sets each sensor's baseline.
2. Then hold smells about the same distance from both sensors for ~10 s each, with clean air
   in between: crushed fresh leaf, dead leaves, hand sanitizer. Plant-like sources matter
   most; ethanol alone will dominate the fit.
3. Writes the fit to sensing.differential.calibration (calibration/crosscal.json) and the raw
   pairs to calibration/crosscal_pairs.csv (the scatter plot for the poster). Restart plantbot to pick it up.
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plantbot import config  # noqa: E402
from plantbot.hal import env  # noqa: E402
from plantbot.sensing import crosscal  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--baseline-s", type=int, default=20)
    ap.add_argument("--mock", action="store_true", help="fake sensors with scripted co-located smells")
    ap.add_argument("--config")
    a = ap.parse_args()
    cfg = config.load(a.config, mock=a.mock)
    e, status = env.make(cfg.env, cfg.mock)
    if status.get("mq2") == "offline" or status.get("bme680") == "offline":
        sys.exit(f"need both sensors, got {status}")
    if a.mock:
        e.events = []  # replace the run's script with co-located smells: both sensors see the same air
        for t in (a.baseline_s + 10, a.baseline_s + 40, a.baseline_s + 70):
            e.events.append({"t": t, "duration": 12, "mq2_dv": 0.35, "gas_scale": 0.6})
    mq2 = cfg.env.mq2
    out_path = cfg.sensing.differential.calibration
    if a.mock:  # never overwrite the real rover's fit with fake data
        out_path = out_path.replace(".json", "_mock.json")
    pairs_path = out_path.replace(".json", "_pairs.csv")

    def rs(v):
        return env.mq2_rs_kohm(v, mq2.vc, mq2.rl_kohm)

    print(f"sensors: {status}. Clean air for {a.baseline_s} s, nothing near the sensors...")
    base_rs, base_gas = [], []
    t_end = time.time() + a.baseline_s
    while time.time() < t_end:
        r = e.read()
        base_rs.append(rs(r.mq2_v))
        if r.gas_ohms:
            base_gas.append(r.gas_ohms)
        time.sleep(1.0)
    if not base_gas:
        sys.exit("BME680 gas never became heat-stable; let it warm up and retry")
    b_rs, b_gas = statistics.median(base_rs), statistics.median(base_gas)
    print(f"baseline: MQ-2 rs={b_rs:.2f} kOhm, BME680 gas={b_gas:.0f} ohm")
    print("Now bring smells to both sensors at equal distance, clean air in between.\n")

    xs, ys, rows = [], [], []
    mq2_window: list[float] = []
    t0 = time.time()
    t_end = t0 + a.seconds - a.baseline_s
    while time.time() < t_end:
        # BME680 updates ~1 Hz; average the MQ-2 over the same second so the two line up.
        mq2_window.clear()
        t_tick = time.time() + 1.0
        while time.time() < t_tick:
            r = e.read()
            mq2_window.append(r.mq2_v)
            time.sleep(0.1)
        x = crosscal.log_ratio(b_rs, rs(statistics.mean(mq2_window)))
        y = crosscal.log_ratio(b_gas, r.gas_ohms)
        if x is None or y is None:
            continue
        xs.append(x)
        ys.append(y)
        rows.append([f"{r.t - t0:.1f}", f"{x:.4f}", f"{y:.4f}", f"{r.rh:.1f}"])
        print(f"{r.t - t0:5.0f}s  lr_mq2={x:+.3f}  lr_bme={y:+.3f}  RH={r.rh:.1f}")

    os.makedirs(os.path.dirname(pairs_path), exist_ok=True)
    with open(pairs_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "lr_mq2", "lr_bme", "rh"])
        w.writerows(rows)

    try:
        cal = crosscal.fit(xs, ys)
    except ValueError as err:
        sys.exit(f"fit failed: {err}")
    r2 = "n/a" if cal.r2 is None else f"{cal.r2:.2f}"
    print(f"\nlr_bme = {cal.a:.3f} * lr_mq2   r2={r2}  n={cal.n}  MQ-2 span={cal.x_span:.3f}")
    if cal.x_span < 0.1:
        print("WARNING: the MQ-2 barely moved. It probably can't see these sources at this "
              "distance, so the differential will be mostly BME680. Try closer or stronger sources.")
    if cal.r2 is None or cal.r2 < 0.5:
        print("WARNING: poor fit (r2 < 0.5). The sensors disagree on these smells; check they "
              "share the same air, or that the BME680 wasn't still warming up.")
    cal.save(out_path)
    print(f"saved {out_path} and {pairs_path}. Restart plantbot to use it.")


if __name__ == "__main__":
    main()
