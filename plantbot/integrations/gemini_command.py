"""Natural-language (and voice) mission commands via Gemini function calling.

"scan the plant on the left then come back" -> [turn left 90, drive forward 30 cm, scan,
return_to_start]; "how is the basil doing?" -> [say "..."] answered from recent scans.

Gemini only *proposes* a plan. Everything it returns goes through validate(): whitelisted actions,
clamped speeds, distances, angles and durations, a cap on the number of steps and the total motion
time. The plan then runs through the same Mission.handle() messages the dashboard sends
({"drive": [l, r]} re-sent every 100 ms, {"cmd": "scan"}), so every existing guard still applies:
drive only works in WARMUP/IDLE and manual mode, the motor watchdog stops the wheels 500 ms after the
last drive message, and E-stop (button, Space, or a new command) aborts the plan at the next tick.

No odometry on the rover (no encoders), so distances and angles are timed moves from the calibration
in config (`gemini.command.cm_per_s`, `deg_per_s`), and return_to_start replays the executed moves
backwards. Good enough on a table, not a field.

Offline / no key: a small keyword parser produces the same action list (source="rules").
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from plantbot.integrations.gemini import DEFAULT_MODEL, _get, load_env_key, make_client

log = logging.getLogger(__name__)

DRIVABLE = {"WARMUP", "IDLE"}  # mirrors mission.DRIVABLE
TICK_S = 0.1

DEFAULTS = {
    "max_actions": 8,
    "max_speed": 0.5,       # 0..1, on top of motors.max_speed (SafeMotors clamps again)
    "drive_speed": 0.4,
    "turn_speed": 0.45,
    "cm_per_s": 12.0,       # (unverified) forward speed at drive_speed; mock: 30 cm/s at 1.0 -> 12 at 0.4
    "deg_per_s": 110.0,     # (unverified) spin rate at turn_speed; measure: time a 360 on the floor
    "max_segment_s": 3.0,   # longest single drive/turn/wait
    "max_motion_s": 15.0,   # total drive+turn time per plan (return_to_start included)
    "max_scans": 3,
    "scan_timeout_s": 40.0,
}

# ---------- tool declarations (the whole whitelist) ----------
TOOLS: list[dict] = [
    {"name": "drive", "description": "Drive straight. Use for 'go forward', 'back up', 'move to the next plant'.",
     "parameters": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["forward", "backward"]},
         "distance_cm": {"type": "number", "description": "1-40 cm. The next plant in a row is ~60 cm away."}},
         "required": ["direction", "distance_cm"]}},
    {"name": "turn", "description": "Spin in place. 'the plant on the left' = turn left 90 first.",
     "parameters": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["left", "right"]},
         "degrees": {"type": "number", "description": "1-180"}},
         "required": ["direction", "degrees"]}},
    {"name": "scan", "description": "Stop, photograph the plant in front, sniff its air (~10 s), score its health.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "wait", "description": "Pause in place.",
     "parameters": {"type": "object", "properties": {"seconds": {"type": "number", "description": "0.1-3"}},
                    "required": ["seconds"]}},
    {"name": "return_to_start", "description": "Undo every drive and turn of this plan, in reverse ('come back').",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "stop", "description": "Stop the wheels now (and end auto patrol).",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "start_patrol", "description": "Start AUTO mode: drive down the row and scan each plant it meets.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "stop_patrol", "description": "End AUTO mode.", "parameters": {"type": "object", "properties": {}}},
    {"name": "say", "description": "Answer the operator: questions about plant health, or why you can't do something. "
                                   "1-2 short sentences.",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
]
ACTIONS = {t["name"] for t in TOOLS}
MOTION = {"drive", "turn"}

SYSTEM = (
    "You control a small plant-health rover on a table or a crop row. Turn the operator's request into "
    "rover actions by calling the provided functions. Emit the COMPLETE plan now, as function calls in the "
    "order they should run; you will not see results. Use only the functions given. Keep plans short. "
    "'the plant on the left/right' means turn that way 90 degrees, drive about 20 cm, then scan. "
    "'come back' means return_to_start. For questions about plants, answer with say(), using only the "
    "recent scans below; if there is no matching scan, say so and suggest scanning. Never invent readings. "
    "If the request is unsafe or impossible, call say() to explain and nothing else."
)


def limits(cfg) -> dict:
    sub = _get(cfg, "command")
    return {k: type(v)(_get(sub, k, v)) for k, v in DEFAULTS.items()}


def _clamp(x, lo: float, hi: float, default: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def validate(calls: list[tuple[str, dict]], lim: dict) -> tuple[list[dict], list[str]]:
    """Proposed (name, args) calls -> safe executable actions. Unknown or malformed calls are dropped."""
    out: list[dict] = []
    errors: list[str] = []
    scans = 0
    motion_s = 0.0
    for name, args in calls:
        args = args if isinstance(args, dict) else {}
        if len(out) >= lim["max_actions"]:
            errors.append(f"plan cut at {lim['max_actions']} steps")
            break
        if name not in ACTIONS:
            errors.append(f"ignored unknown action {name!r}")
            continue
        if name == "drive":
            d = str(args.get("direction", "forward")).lower()
            if d not in ("forward", "backward"):
                errors.append(f"ignored drive direction {d!r}")
                continue
            speed = min(lim["drive_speed"], lim["max_speed"])
            cm = _clamp(args.get("distance_cm"), 1, 40, 10)
            secs = min(cm / lim["cm_per_s"], lim["max_segment_s"])
            a = {"action": "drive", "direction": d, "distance_cm": round(secs * lim["cm_per_s"], 1),
                 "speed": speed, "seconds": round(secs, 2)}
        elif name == "turn":
            d = str(args.get("direction", "left")).lower()
            if d not in ("left", "right"):
                errors.append(f"ignored turn direction {d!r}")
                continue
            deg = _clamp(args.get("degrees"), 1, 180, 90)
            secs = min(deg / lim["deg_per_s"], lim["max_segment_s"])
            a = {"action": "turn", "direction": d, "degrees": round(secs * lim["deg_per_s"]),
                 "speed": min(lim["turn_speed"], lim["max_speed"]), "seconds": round(secs, 2)}
        elif name == "wait":
            a = {"action": "wait", "seconds": round(_clamp(args.get("seconds"), 0.1, lim["max_segment_s"], 1.0), 2)}
        elif name == "scan":
            if scans >= lim["max_scans"]:
                errors.append(f"only {lim['max_scans']} scans per command")
                continue
            scans += 1
            a = {"action": "scan"}
        elif name == "say":
            text = str(args.get("text", "")).strip()[:300]
            if not text:
                continue
            a = {"action": "say", "text": text}
        else:
            a = {"action": name}
        if a["action"] == "return_to_start":
            a["seconds"] = round(sum(x["seconds"] for x in out if x["action"] in MOTION), 2)
        if a["action"] in MOTION or a["action"] == "return_to_start":
            if motion_s + a["seconds"] > lim["max_motion_s"] + 1e-6:
                errors.append(f"plan cut: more than {lim['max_motion_s']:.0f} s of driving")
                break
            motion_s += a["seconds"]
        out.append(a)
    return out, errors


# ---------- offline fallback ----------
_NUM = r"(\d+(?:\.\d+)?)"


def rule_plan(text: str, recent: list[dict] | None = None) -> list[tuple[str, dict]]:
    """Keyword parser for when Gemini is off. Handles the demo phrases, not English."""
    t = text.lower().strip()
    if re.search(r"\b(how|what|is|are|status|doing|healthy|sick)\b.*\?|^how\b|^what\b|\bdoing\b", t) \
            and not re.search(r"\b(scan|drive|go|turn|move)\b", t):
        return [("say", {"text": answer_from_scans(t, recent or [])})]
    calls: list[tuple[str, dict]] = []
    for part in re.split(r"\bthen\b|,|;|\band\b", t):
        p = part.strip()
        if not p:
            continue
        if re.search(r"\b(stop|halt|freeze)\b", p):
            calls.append(("stop" if "patrol" not in p else "stop_patrol", {}))
        elif re.search(r"\b(patrol|auto)\b", p):
            calls.append(("start_patrol", {}))
        elif re.search(r"\b(come back|return|go back|home)\b", p):
            calls.append(("return_to_start", {}))
        elif m := re.search(r"\b(left|right)\b", p):
            deg = re.search(_NUM + r"\s*(?:deg|°)", p)
            calls.append(("turn", {"direction": m.group(1), "degrees": float(deg.group(1)) if deg else 90}))
            if re.search(r"\bplant\b", p):
                calls.append(("drive", {"direction": "forward", "distance_cm": 20}))
            if "scan" in p:
                calls.append(("scan", {}))
        elif re.search(r"\b(back up|backward|reverse)\b", p):
            cm = re.search(_NUM + r"\s*cm", p)
            calls.append(("drive", {"direction": "backward", "distance_cm": float(cm.group(1)) if cm else 15}))
        elif re.search(r"\b(forward|ahead|next plant|drive|go)\b", p):
            cm = re.search(_NUM + r"\s*cm", p)
            calls.append(("drive", {"direction": "forward", "distance_cm": float(cm.group(1)) if cm else 20}))
            if "scan" in p:
                calls.append(("scan", {}))
        elif re.search(r"\b(scan|check|look at|inspect)\b", p):
            calls.append(("scan", {}))
        elif m := re.search(r"\bwait\s*" + _NUM, p):
            calls.append(("wait", {"seconds": float(m.group(1))}))
    return calls or [("say", {"text": "Sorry, I didn't understand. Try 'scan the plant on the left then come back'."})]


def answer_from_scans(question: str, recent: list[dict]) -> str:
    if not recent:
        return "I haven't scanned anything yet. Say 'scan' to check the plant in front of me."
    s = recent[0]
    d = s.get("diagnosis") or {}
    name = d.get("species_guess") or "The last plant"
    if d:
        return f"{name.capitalize()}: {d.get('health')} ({d.get('likely_issue')}). {d.get('explain_for_farmer', '')}".strip()
    return f"The last plant I scanned scored {s.get('health', 0):.0f}/100 ({s.get('status')}): {s.get('explain', '')}."


# ---------- planner + executor ----------
class Commander:
    def __init__(self, mission, cfg=None, doctor=None, mock_mode: bool = False):
        self.m = mission
        self.cfg = cfg
        self.doctor = doctor
        self.lim = limits(cfg)
        self.model = _get(cfg, "command_model") or _get(cfg, "model", DEFAULT_MODEL)
        self.timeout_s = float(_get(cfg, "timeout_s", 15))
        self.key = load_env_key()
        mk = _get(cfg, "mock", "auto")
        self.use_rules = (not bool(_get(cfg, "enabled", True)) or mk is True
                          or (mk == "auto" and mock_mode and not self.key) or not self.key)
        self.client = None
        self.task: asyncio.Task | None = None
        self.abort = False
        self.state: dict[str, Any] = {"running": False, "plan": [], "step": None, "log": [], "text": None}
        self.last_error: str | None = None

    def status(self) -> dict:
        return {**self.state, "planner": "rules" if self.use_rules else f"gemini:{self.model}",
                "error": self.last_error, "limits": self.lim}

    # ----- context for questions -----
    def recent_scans(self, n: int = 5) -> list[dict]:
        try:
            rows = self.m.r.store.list(n)
        except Exception:
            return []
        out = []
        for r in rows:
            d = self.doctor.cached(r["id"]) if self.doctor else None
            out.append({"id": r["id"], "when": time.strftime("%H:%M", time.localtime(r["t"])),
                        "health": r.get("health"), "status": r.get("status"), "cnn_label": r.get("label"),
                        "explain": r.get("explain") or "", "diagnosis": {k: d.get(k) for k in (
                            "species_guess", "health", "likely_issue", "urgency", "explain_for_farmer")} if d else None})
        return out

    # ----- planning (blocking: call in a thread) -----
    def plan(self, text: str | None = None, audio: bytes | None = None, mime: str | None = None) -> dict:
        recent = self.recent_scans()
        source, calls, heard = "rules", None, text
        if not self.use_rules:
            calls, heard = self._gemini_calls(text, audio, mime, recent)
            if calls is not None:
                source = "gemini"
        if calls is None:
            if not text:
                return {"actions": [], "errors": ["voice needs Gemini (no key or offline): type the command"],
                        "source": "rules", "text": None}
            calls = rule_plan(text, recent)
        actions, errors = validate(calls, self.lim)
        return {"actions": actions, "errors": errors, "source": source, "text": heard}

    def _gemini_calls(self, text, audio, mime, recent) -> tuple[list[tuple[str, dict]] | None, str | None]:
        try:
            from google.genai import types
        except ImportError:
            self.last_error = "pip install google-genai"
            return None, text
        try:
            if self.client is None:
                self.client = make_client(self.key, self.timeout_s)
            ctx = (f"Rover state: {self.m.state}, mode {self.m.mode}. Calibration: drive {self.lim['cm_per_s']:.0f} "
                   f"cm/s, max 40 cm per drive call.\nRecent scans (newest first): {json.dumps(recent, default=str)}")
            parts: list[Any] = [ctx]
            if audio:
                parts += [types.Part.from_bytes(data=audio, mime_type=(mime or "audio/webm").split(";")[0]),
                          "The operator's spoken command is in this audio clip. First call say() only if it is a "
                          "question; otherwise just call the actions."]
            if text:
                parts.append(f"Operator: {text}")
            cfg = types.GenerateContentConfig(
                system_instruction=SYSTEM, temperature=0.1, max_output_tokens=1024,
                tools=[types.Tool(function_declarations=[
                    types.FunctionDeclaration(name=t["name"], description=t["description"],
                                              parameters_json_schema=t["parameters"]) for t in TOOLS])],
                tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.ANY, allowed_function_names=sorted(ACTIONS))),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
            resp = self.client.models.generate_content(model=self.model, contents=parts, config=cfg)
            calls = [(fc.name, dict(fc.args or {})) for fc in (resp.function_calls or [])]
            if not calls and (resp.text or "").strip():
                calls = [("say", {"text": resp.text.strip()})]
            self.last_error = None
            return calls, text or "(voice)"
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            log.warning("Gemini command planning failed, using keyword rules: %s", self.last_error)
            return None, text

    # ----- execution -----
    async def command(self, text: str | None = None, audio: bytes | None = None, mime: str | None = None,
                      execute: bool = True) -> dict:
        self.cancel()  # a new command replaces the old one
        p = await asyncio.wait_for(asyncio.to_thread(self.plan, text, audio, mime), self.timeout_s + 5)
        log.info("command %r -> %s via %s %s", p["text"], [a["action"] for a in p["actions"]], p["source"],
                 p["errors"] or "")
        if execute and p["actions"]:
            self.task = asyncio.create_task(self.execute(p["actions"], p["text"]))
        return p

    def cancel(self) -> None:
        self.abort = True
        if self.task and not self.task.done():
            self.task.cancel()

    def _blocked(self) -> str | None:
        if self.abort:
            return "cancelled"
        if self.m.state == "ESTOP":
            return "E-stop"
        return None

    async def _move(self, l: float, r: float, secs: float) -> str | None:
        if self.m.state not in DRIVABLE or self.m.mode != "manual":
            return f"can't drive in {self.m.state}/{self.m.mode}"
        t_end = time.monotonic() + secs
        try:
            while time.monotonic() < t_end:
                if why := self._blocked():
                    return why
                await self.m.handle({"drive": [l, r]})  # re-sent every tick: the watchdog stays fed
                await asyncio.sleep(TICK_S)
        finally:
            if self.m.state in DRIVABLE:
                await self.m.handle({"drive": [0.0, 0.0]})
        return None

    async def _scan(self) -> str | None:
        if self.m.state not in DRIVABLE or self.m._busy():
            return f"can't scan in {self.m.state}"
        await self.m.handle({"cmd": "scan"})
        t_end = time.monotonic() + self.lim["scan_timeout_s"]
        while self.m._busy():
            if why := self._blocked():
                return why
            if time.monotonic() > t_end:
                return "scan timed out"
            await asyncio.sleep(TICK_S)
        return None

    @staticmethod
    def _wheels(a: dict) -> tuple[float, float]:
        s = a["speed"]
        if a["action"] == "drive":
            return (s, s) if a["direction"] == "forward" else (-s, -s)
        return (-s, s) if a["direction"] == "left" else (s, -s)

    async def execute(self, actions: list[dict], text: str | None = None) -> dict:
        self.abort = False
        st = self.state = {"running": True, "plan": actions, "step": 0, "log": [], "text": text, "t": time.time()}
        done: list[dict] = []  # executed motion, for return_to_start

        def note(msg: str) -> None:
            st["log"].append(msg)

        try:
            for i, a in enumerate(actions):
                st["step"] = i
                if why := self._blocked():
                    note(f"stopped: {why}")
                    break
                act, err = a["action"], None
                if act in MOTION:
                    err = await self._move(*self._wheels(a), a["seconds"])
                    if not err:
                        done.append(a)
                elif act == "return_to_start":
                    for b in reversed(done):
                        inv = {**b, "direction": {"forward": "backward", "backward": "forward",
                                                  "left": "right", "right": "left"}[b["direction"]]}
                        if err := await self._move(*self._wheels(inv), b["seconds"]):
                            break
                    done.clear()
                elif act == "scan":
                    err = await self._scan()
                    if not err and self.m.last_scan:
                        ls = self.m.last_scan
                        note(f"scanned {ls['id']}: {ls['status']} {ls['health']:.0f}")
                elif act == "wait":
                    t_end = time.monotonic() + a["seconds"]
                    while time.monotonic() < t_end and not (err := self._blocked()):
                        await asyncio.sleep(TICK_S)
                elif act == "stop":
                    if self.m.mode == "auto":
                        await self.m.handle({"cmd": "auto_off"})
                    if self.m.state in DRIVABLE:
                        await self.m.handle({"drive": [0.0, 0.0]})
                elif act == "start_patrol":
                    await self.m.handle({"cmd": "auto_on"})
                elif act == "stop_patrol":
                    await self.m.handle({"cmd": "auto_off"})
                elif act == "say":
                    note("rover: " + a["text"])
                    continue
                if err:
                    note(f"step {i + 1} ({act}) stopped: {err}")
                    break
                note(f"step {i + 1}: {act} ok")
        except asyncio.CancelledError:
            note("cancelled")
        finally:
            st["running"] = False
            st["step"] = None
            if self.m.state in DRIVABLE and self.m.mode == "manual":
                await self.m.handle({"drive": [0.0, 0.0]})
        return st
