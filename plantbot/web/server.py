"""FastAPI: dashboard, MJPEG stream, websocket control + telemetry, scan history."""
from __future__ import annotations

import asyncio
import json
import logging
import math
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def _clean(o):
    """NaN isn't valid JSON; send null instead."""
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def make_app(mission) -> FastAPI:
    robot = mission.r
    clients: set[WebSocket] = set()

    @asynccontextmanager
    async def lifespan(app):
        tasks = [asyncio.create_task(mission.run()), asyncio.create_task(telemetry_loop())]
        md = robot.cfg.get("mouse_drive")
        if md and md.get("enabled") and not robot.cfg.mock_mode:
            from plantbot.hal import mouse_drive

            tasks.append(asyncio.create_task(mouse_drive.run(mission, md)))
        yield
        for t in tasks:
            t.cancel()
        robot.motors.stop()

    app = FastAPI(title="plantbot", lifespan=lifespan)

    async def broadcast(msg: dict) -> None:
        data = json.dumps(_clean(msg), default=str)
        for ws in list(clients):
            try:
                await ws.send_text(data)
            except Exception:
                clients.discard(ws)

    mission.broadcast = broadcast

    async def telemetry_loop():
        while True:
            if clients:
                await broadcast({"telemetry": mission.telemetry()})
            await asyncio.sleep(0.2)  # 5 Hz

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/stream.mjpg")
    async def stream():
        period = 1.0 / robot.cfg.camera.stream_fps

        async def frames():
            while True:
                jpg = robot.camera.lores_jpeg()
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
                await asyncio.sleep(period)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        clients.add(sock)
        log.info("ws client connected (%d)", len(clients))
        await sock.send_text(json.dumps(_clean({"telemetry": mission.telemetry(), "last_scan": mission.last_scan,
                                                "config": {"watchdog_ms": robot.cfg.motors.watchdog_ms}}), default=str))
        try:
            while True:
                msg = json.loads(await sock.receive_text())
                await mission.handle(msg)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("ws error")
        finally:
            clients.discard(sock)
            log.info("ws client gone (%d left)", len(clients))
            # No explicit stop here: the motor watchdog handles a dropped connection.

    @app.get("/api/status")
    async def status():
        return _clean(mission.telemetry())

    @app.get("/api/scans")
    async def scans(limit: int = 100):
        return _clean(robot.store.list(limit))

    @app.get("/api/scans/{scan_id}")
    async def scan(scan_id: str):
        rec = robot.store.get(scan_id)
        if not rec:
            raise HTTPException(404)
        return _clean(rec)

    app.mount("/scans", StaticFiles(directory=robot.store.root), name="scans")
    return app
