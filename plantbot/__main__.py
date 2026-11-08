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
    from plantbot.leaflink import LeafLink

    ll = LeafLink(cfg.get("leaflink") or config.Cfg(), sampler)
    devices["leaflink"] = "ok" if ll.enabled else "disabled"
    ll.start()
    return Robot(cfg, m, cam, e, sampler, rng, f, clf, Store(cfg.store.dir), devices, ll)


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
