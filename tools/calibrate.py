#!/usr/bin/env python3
"""Print MQ-2 / BME680 clean-air values for N seconds, so you can sanity-check warm-up and RL.

    python tools/calibrate.py --seconds 120
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plantbot import config  # noqa: E402
from plantbot.hal import env  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--config")
    a = ap.parse_args()
    cfg = config.load(a.config, mock=a.mock)
    e, status = env.make(cfg.env, cfg.mock)
    print(f"sensors: {status}. Keep the pod in clean air, away from plants.")
    mq, gas = [], []
    t_end = time.time() + a.seconds
    while time.time() < t_end:
        r = e.read()
        rs = env.mq2_rs_kohm(r.mq2_v, cfg.env.mq2.vc, cfg.env.mq2.rl_kohm)
        mq.append(r.mq2_v)
        if r.gas_ohms:
            gas.append(r.gas_ohms)
        print(f"{a.seconds - (t_end - time.time()):5.0f}s  mq2={r.mq2_v:.3f} V  rs={rs:.2f} kOhm  gas={r.gas_ohms}  RH={r.rh:.1f}")
        time.sleep(1.0)
    last = mq[-10:]
    print(f"\nMQ-2 last 10 s: mean {statistics.mean(last):.3f} V, spread {max(last) - min(last):.3f} V")
    if gas:
        print(f"BME680 gas last 10: mean {statistics.mean(gas[-10:]):.0f} ohm")
    print("Stable (spread < ~0.01 V)? Then sensing.warmup_s in config.yaml can be about this long.")


if __name__ == "__main__":
    main()
