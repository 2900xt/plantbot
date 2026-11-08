"""Gemini routes: Plant Doctor card + natural-language commands. Wired with one call in make_app():

    from plantbot.web.gemini_routes import install_gemini
    install_gemini(app, mission)      # after `mission.broadcast = broadcast`

GET  /api/gemini/status              doctor + commander status
GET  /api/gemini/doctor/latest       newest scan + its diagnosis (runs Gemini if not cached yet)
GET  /api/gemini/doctor/{id}?refresh=1
POST /api/gemini/command             {"text": "...", "execute": true} or {"audio_b64": "...", "mime": "audio/webm"}
GET  /api/gemini/command             running plan, step, log
POST /api/gemini/command/cancel

Every finished scan is diagnosed in the background and pushed to websocket clients as
{"diagnosis": {...}}; the scan itself is never delayed.
"""
from __future__ import annotations

import asyncio
import base64
import logging

from fastapi import Body, HTTPException

from plantbot.integrations.gemini import PlantDoctor
from plantbot.integrations.gemini_command import Commander

log = logging.getLogger(__name__)
MAX_AUDIO_B = 2_000_000  # ~1 min of opus; Gemini inline data limit is far above this


def install_gemini(app, mission) -> tuple[PlantDoctor, Commander]:
    robot = mission.r
    gcfg = robot.cfg.get("gemini")
    mock = bool(robot.cfg.get("mock_mode"))
    doctor = PlantDoctor(gcfg, mock_mode=mock, store_root=robot.store.root)
    commander = Commander(mission, gcfg, doctor=doctor, mock_mode=mock)
    robot.doctor, robot.commander = doctor, commander  # handy from a REPL / other modules
    auto = bool(getattr(gcfg, "auto_diagnose", True)) if gcfg is not None else True
    pending: set[asyncio.Task] = set()

    send = mission.broadcast

    async def diagnose_and_push(rec: dict) -> None:
        d = await doctor.diagnose_scan_async(rec)
        if d:
            await send({"diagnosis": d})

    async def broadcast(msg: dict) -> None:
        await send(msg)
        if auto and doctor.available and isinstance(msg.get("scan"), dict):
            t = asyncio.create_task(diagnose_and_push(msg["scan"]))  # never awaited by the scan
            pending.add(t)
            t.add_done_callback(pending.discard)

    mission.broadcast = broadcast

    @app.get("/api/gemini/status")
    async def gemini_status():
        return {"doctor": doctor.status(), "command": commander.status()}

    async def _with_diag(rec: dict, refresh: bool = False) -> dict:
        d = doctor.cached(rec["id"]) if not refresh else None
        if d is None and doctor.available:
            d = await doctor.diagnose_scan_async(rec, refresh=refresh)
        return {"scan": rec, "diagnosis": d, "doctor": doctor.status()}

    @app.get("/api/gemini/doctor/latest")
    async def doctor_latest():
        rows = robot.store.list(1)
        if not rows:
            return {"scan": None, "diagnosis": None, "doctor": doctor.status()}
        rec = robot.store.get(rows[0]["id"]) or rows[0]
        return await _with_diag(rec)

    @app.get("/api/gemini/doctor/{scan_id}")
    async def doctor_scan(scan_id: str, refresh: bool = False):
        rec = robot.store.get(scan_id)
        if not rec:
            raise HTTPException(404)
        return await _with_diag(rec, refresh)

    @app.post("/api/gemini/command")
    async def command(body: dict = Body(...)):
        text = (body.get("text") or "").strip()[:500] or None
        audio = None
        if body.get("audio_b64"):
            try:
                audio = base64.b64decode(body["audio_b64"], validate=True)
            except ValueError:
                raise HTTPException(400, "bad audio_b64")
            if len(audio) > MAX_AUDIO_B:
                raise HTTPException(413, "audio too long")
        if not text and not audio:
            raise HTTPException(400, "send text or audio_b64")
        try:
            return await commander.command(text, audio, body.get("mime"), execute=bool(body.get("execute", True)))
        except asyncio.TimeoutError:
            raise HTTPException(504, "planner timed out")

    @app.get("/api/gemini/command")
    async def command_status():
        return commander.status()

    @app.post("/api/gemini/command/cancel")
    async def command_cancel():
        commander.cancel()
        if mission.state in ("WARMUP", "IDLE") and mission.mode == "manual":
            await mission.handle({"drive": [0.0, 0.0]})
        return commander.status()

    log.info("Gemini: doctor %s, commands via %s", "on" if doctor.available else "off",
             commander.status()["planner"])
    return doctor, commander
