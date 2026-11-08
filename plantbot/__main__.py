"""python -m plantbot [--mock] [--config path] [--camera folder|opencv|picamera] [--port N]"""
from __future__ import annotations

import argparse
import atexit
import logging
import os
import socket

from plantbot import config


def build_robot(cfg):
    from plantbot.hal import camera, env, fan, motors, ranger
    from plantbot.mission import Robot
    from plantbot.sensing.sampler import Sampler
    from plantbot.store import Store
    from plantbot.vision import classify

    devices = {}
    m, devices["motors"] = motors.make(cfg.motors)
    cam, devices["camera"] = camera.make(cfg.camera, cfg.mock)
    e, env_status = env.make(cfg.env, cfg.mock)
    devices.update(env_status)
    rng, devices["ranger"] = ranger.make(cfg.ranger, cfg.mock, m)
    f, devices["fan"] = fan.make(cfg.fan)
    clf, devices["classifier"] = classify.make(cfg.classifier)
    sampler = Sampler(e, cfg.sensing, cfg.env.mq2, csv_path=os.path.join(cfg.store.dir, "sensors.csv"))
    if cfg.env.driver in (None, "none"):
        sampler._set_warm("no env sensors fitted")  # nothing to warm up; don't sit in WARMUP
    from plantbot.leaflink import LeafLink

    ll = LeafLink(cfg.get("leaflink") or config.Cfg(), sampler)
    devices["leaflink"] = "ok" if ll.enabled else "disabled"
    ll.start()
    from plantbot.integrations.sinks import build_fanout

    sinks = build_fanout(cfg, sampler, store_root=cfg.store.dir)  # no-op unless TIGERDATA_URL / SNOWFLAKE_* / SOLANA_KEYPAIR
    devices["sinks"] = ",".join(r.sink.name for r in sinks.runners) or "disabled"
    sinks.start()

    from plantbot.hal import probe_arm, tactile
    from plantbot.integrations.zelos_pub import ZelosPublisher
    from plantbot.sensing.bumper import ContactGuard, GuardedMotors

    tcfg = cfg.get("tactile")
    tac, devices["tactile"] = tactile.make(tcfg)
    if hasattr(tac, "set_leaf") and cfg.mock.get("leaf_turgor") is not None:
        tac.set_leaf(cfg.mock.leaf_turgor)
    arm, devices["probe_arm"] = probe_arm.make(tcfg.get("arm") if tcfg else None, tac, mock=cfg.mock_mode)
    guard = ContactGuard(tac, m, tcfg.get("bumper") if tcfg else None,
                         csv_path=os.path.join(cfg.store.dir, "contacts.csv"))
    m = GuardedMotors(m, guard)  # bumper: forward drive refused while the nose is touching something
    devices["bumper"] = "armed" if guard.armed and devices["tactile"] in ("ok", "mock") else "off"
    zel = ZelosPublisher(cfg.get("zelos"), tactile=tac, guard=guard)
    devices["zelos"] = "ok" if zel.enabled else (zel.error or "disabled")
    return Robot(cfg, m, cam, e, sampler, rng, f, clf, Store(cfg.store.dir), devices, ll, sinks,
                 tactile=tac, arm=arm, guard=guard, zelos=zel)


def _ips() -> list[str]:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packets sent; picks the outbound interface
        return [s.getsockname()[0]]
    except OSError:
        return []


def main() -> None:
    ap = argparse.ArgumentParser(prog="plantbot")
    ap.add_argument("--mock", action="store_true", help="fake every device; runs on a laptop")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--camera", choices=["folder", "opencv", "picamera"], help="override camera driver")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("plantbot")

    cfg = config.load(a.config, mock=a.mock, camera=a.camera)
    robot = build_robot(cfg)
    atexit.register(robot.close)
    log.info("devices: %s", robot.devices)

    import uvicorn

    from plantbot.mission import Mission
    from plantbot.web.server import make_app

    mission = Mission(robot)
    robot.guard.on_trip = mission.on_bump_trip      # tactile reader thread -> mission
    robot.guard.on_event = mission.on_bump
    robot.zelos.mission = mission
    robot.zelos.start()
    app = make_app(mission)
    host, port = a.host or cfg.server.host, a.port or cfg.server.port
    for ip in _ips() + ["localhost"]:
        log.info("dashboard: http://%s:%d/", ip, port)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        robot.motors.stop()


if __name__ == "__main__":
    main()
