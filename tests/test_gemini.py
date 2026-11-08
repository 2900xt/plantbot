"""Gemini Plant Doctor + NL commands. Mock mode and fakes only: no network, no API key needed.

    .venv/bin/python -m pytest tests/test_gemini.py -q
"""
from __future__ import annotations

import asyncio
import json
import logging
import types as pytypes

import pytest

from plantbot.integrations import gemini as gm
from plantbot.integrations import gemini_command as gc

ENV_CLEAN = {"gas_ratio": 0.98, "mq2_ratio": 0.95, "diff_ratio": 0.97, "temp_c": 22.5, "rh": 45, "gas_ohms": 118000}
ENV_SMELLY = {**ENV_CLEAN, "gas_ratio": 0.62, "diff_ratio": 0.7}
KEYS = {"species_guess", "health", "likely_issue", "evidence", "confidence", "care_actions", "urgency",
        "explain_for_farmer"}


@pytest.fixture(autouse=True)
def no_key(monkeypatch, tmp_path):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(gm, "ROOT", tmp_path)  # no repo .env either
    monkeypatch.setattr(gc, "TICK_S", 0.01)


# ---------- Plant Doctor ----------
def test_mock_diagnosis_is_schema_valid_and_tracks_inputs(tmp_path):
    (tmp_path / "s1").mkdir()
    doc = gm.PlantDoctor({"mock": True}, store_root=str(tmp_path))
    d = doc.diagnose("s1", b"jpeg", "leaf_spot", 0.7, ENV_SMELLY)
    assert KEYS <= d.keys()
    assert d["health"] == "diseased" and d["urgency"] == "high" and d["source"] == "mock"
    assert 0 <= d["confidence"] <= 1 and d["explain_for_farmer"]
    assert doc.diagnose("s2", None, "healthy", 0.9, ENV_CLEAN)["health"] == "healthy"
    assert doc.diagnose("s3", None, "healthy", 0.9, ENV_SMELLY)["health"] == "stressed"
    # cached in memory and on disk next to the scan
    assert doc.diagnose("s1", b"jpeg", "healthy", 0.9, ENV_CLEAN) is d
    assert json.loads((tmp_path / "s1" / "gemini.json").read_text())["likely_issue"] == d["likely_issue"]
    fresh = gm.PlantDoctor({"mock": True}, store_root=str(tmp_path))
    assert fresh.cached("s1")["health"] == "diseased"


def test_mock_auto_only_in_mock_mode_without_key():
    assert gm.PlantDoctor({"mock": "auto"}, mock_mode=True).mock
    assert not gm.PlantDoctor({"mock": "auto"}, mock_mode=False).mock


def test_no_key_returns_none_and_logs_once(caplog):
    caplog.set_level(logging.WARNING)
    doc = gm.PlantDoctor({"mock": False})
    assert not doc.available
    assert doc.diagnose("x", b"", "healthy", 0.9, ENV_CLEAN) is None
    assert doc.diagnose("y", b"", "healthy", 0.9, ENV_CLEAN) is None
    assert sum("GEMINI_API_KEY" in r.message for r in caplog.records) == 1


def test_key_from_dotenv(tmp_path):
    (tmp_path / ".env").write_text("LEAFLINK_URL=http://x\nexport GEMINI_API_KEY='abc123'\n")
    assert gm.load_env_key() == "abc123"


def test_normalize_clamps_bad_model_output():
    d = gm.normalize({"health": "DYING", "confidence": 7, "urgency": "asap", "evidence": "one string",
                      "care_actions": [str(i) for i in range(9)]})
    assert d["health"] == "stressed" and d["confidence"] == 1.0 and d["urgency"] == "medium"
    assert d["evidence"] == ["one string"] and len(d["care_actions"]) == 4 and d["species_guess"] == "unknown"


def test_diagnose_scan_reads_crop_and_record(tmp_path):
    (tmp_path / "abc").mkdir()
    (tmp_path / "abc" / "crop.jpg").write_bytes(b"\xff\xd8crop")
    doc = gm.PlantDoctor({"mock": True}, store_root=str(tmp_path))
    rec = {"id": "abc", "label": "blight", "probs": {"healthy": 0.2, "blight": 0.8}, "p_healthy": 0.2,
           "voc": {"gas_ratio": 0.6, "gas_ohms": 70000}, "temp_c": 25, "rh": 60}
    jpeg, label, conf, env = doc.inputs_from_scan(rec)
    assert jpeg == b"\xff\xd8crop" and label == "blight" and conf == 0.8 and env["rh"] == 60
    d = asyncio.run(doc.diagnose_scan_async(rec))
    assert d["likely_issue"] == "early blight" and d["cnn"]["label"] == "blight"


class _FakeModels:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def generate_content(self, **kw):
        self.calls.append(kw)
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def test_real_path_with_fake_client(monkeypatch):
    pytest.importorskip("google.genai")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    doc = gm.PlantDoctor({"mock": False, "model": "gemini-test"})
    out = {"species_guess": "tomato", "health": "diseased", "likely_issue": "early blight",
           "evidence": ["concentric brown rings"], "confidence": 0.8, "care_actions": ["remove leaves"],
           "urgency": "high", "explain_for_farmer": "Blight. Remove the bad leaves."}
    fake = _FakeModels(pytypes.SimpleNamespace(parsed=out, text=json.dumps(out)))
    doc.client = pytypes.SimpleNamespace(models=fake)
    d = doc.diagnose("t1", b"\xff\xd8img", "leaf_spot", 0.6, ENV_SMELLY)
    assert d["source"] == "gemini" and d["species_guess"] == "tomato" and d["model"] == "gemini-test"
    kw = fake.calls[0]
    assert kw["model"] == "gemini-test"
    assert kw["config"].response_json_schema == gm.DIAGNOSIS_SCHEMA
    assert kw["config"].response_mime_type == "application/json"
    assert kw["contents"][0].inline_data.mime_type == "image/jpeg"
    # offline: returns None, rover carries on
    doc2 = gm.PlantDoctor({"mock": False})
    doc2.client = pytypes.SimpleNamespace(models=_FakeModels(TimeoutError("offline")))
    assert doc2.diagnose("t2", b"", "healthy", 0.9, ENV_CLEAN) is None and "offline" in doc2.last_error


# ---------- command validation ----------
LIM = gc.limits({"command": {"cm_per_s": 200, "deg_per_s": 2000}})


def test_validate_whitelists_and_clamps():
    acts, errs = gc.validate([
        ("drive", {"direction": "forward", "distance_cm": 9999}),
        ("self_destruct", {}),
        ("turn", {"direction": "up", "degrees": 90}),
        ("turn", {"direction": "left", "degrees": -50}),
        ("wait", {"seconds": 600}),
        ("say", {"text": "hi"}),
    ], gc.limits(None))
    names = [a["action"] for a in acts]
    assert names == ["drive", "turn", "wait", "say"]
    drive = acts[0]
    assert drive["distance_cm"] <= 40 and drive["speed"] <= 0.5 and drive["seconds"] <= 3
    assert acts[1]["degrees"] >= 1 and acts[2]["seconds"] == 3
    assert any("self_destruct" in e for e in errs) and any("up" in e for e in errs)


def test_validate_caps_steps_scans_and_motion():
    acts, errs = gc.validate([("scan", {})] * 5, gc.limits(None))
    assert len(acts) == 3 and errs
    lim = gc.limits({"command": {"max_motion_s": 4}})
    acts, errs = gc.validate([("drive", {"direction": "forward", "distance_cm": 40})] * 3, lim)
    assert sum(a["seconds"] for a in acts) <= 4 and any("driving" in e for e in errs)
    acts, _ = gc.validate([("wait", {"seconds": 0.1})] * 20, gc.limits(None))
    assert len(acts) == gc.DEFAULTS["max_actions"]


def test_return_to_start_counts_toward_motion_budget():
    lim = gc.limits({"command": {"max_motion_s": 5, "cm_per_s": 10}})
    acts, errs = gc.validate([("drive", {"direction": "forward", "distance_cm": 30}), ("return_to_start", {})], lim)
    assert [a["action"] for a in acts] == ["drive"] and errs  # 3 s out + 3 s back > 5 s


def test_rule_plan_demo_phrases():
    calls = gc.rule_plan("scan the plant on the left then come back")
    assert [c[0] for c in calls] == ["turn", "drive", "scan", "return_to_start"]
    assert calls[0][1]["direction"] == "left"
    q = gc.rule_plan("how is the basil doing?", [{"health": 42, "status": "watch", "explain": "x",
                                                  "diagnosis": {"species_guess": "basil", "health": "stressed",
                                                                "likely_issue": "underwatering",
                                                                "explain_for_farmer": "Water it."}}])
    assert q[0][0] == "say" and "underwatering" in q[0][1]["text"]
    assert gc.rule_plan("stop")[0][0] == "stop"
    assert gc.rule_plan("drive forward 30 cm and scan") == [("drive", {"direction": "forward", "distance_cm": 30.0}),
                                                           ("scan", {})]


# ---------- executor against a fake mission ----------
class FakeMission:
    def __init__(self, state="IDLE"):
        self.state, self.mode = state, "manual"
        self.sent: list = []
        self.task = None
        self.last_scan = None
        self.r = pytypes.SimpleNamespace(store=pytypes.SimpleNamespace(list=lambda n: []))

    async def handle(self, msg):
        self.sent.append(msg)
        if msg.get("cmd") == "scan":
            async def scan():
                await asyncio.sleep(0.05)
                self.last_scan = {"id": "s9", "status": "healthy", "health": 91.0}
            self.task = asyncio.create_task(scan())

    def _busy(self):
        return self.task is not None and not self.task.done()

    def drives(self):
        return [tuple(m["drive"]) for m in self.sent if "drive" in m]


def test_executor_runs_plan_and_returns(tmp_path):
    m = FakeMission()
    cmd = gc.Commander(m, {"mock": True, "command": {"cm_per_s": 200, "deg_per_s": 2000}}, mock_mode=True)

    async def go():
        p = await cmd.command("scan the plant on the left then come back")
        await cmd.task
        return p

    p = asyncio.run(go())
    assert p["source"] == "rules" and [a["action"] for a in p["actions"]] == ["turn", "drive", "scan", "return_to_start"]
    d = m.drives()
    assert (-0.45, 0.45) in d and (0.4, 0.4) in d  # left spin, forward
    assert (-0.4, -0.4) in d and (0.45, -0.45) in d  # undo: back up, spin right
    assert d[-1] == (0.0, 0.0)
    assert {"cmd": "scan"} in m.sent
    assert m.sent.index({"drive": [-0.4, -0.4]}) > m.sent.index({"cmd": "scan"})  # reverse only after the scan
    assert any("scanned s9" in line for line in cmd.state["log"]) and not cmd.state["running"]


def test_executor_aborts_on_estop():
    m = FakeMission()
    cmd = gc.Commander(m, {"mock": True, "command": {"cm_per_s": 10}}, mock_mode=True)

    async def go():
        actions, _ = gc.validate([("drive", {"direction": "forward", "distance_cm": 30}), ("scan", {})], cmd.lim)
        t = asyncio.create_task(cmd.execute(actions))
        await asyncio.sleep(0.1)
        m.state = "ESTOP"
        return await t

    st = asyncio.run(go())
    assert {"cmd": "scan"} not in m.sent
    assert any("E-stop" in line for line in st["log"])


def test_executor_refuses_to_drive_outside_manual_idle():
    m = FakeMission(state="CRUISE")
    m.mode = "auto"
    cmd = gc.Commander(m, {"mock": True}, mock_mode=True)
    acts, _ = gc.validate([("drive", {"direction": "forward", "distance_cm": 10})], cmd.lim)
    st = asyncio.run(cmd.execute(acts))
    assert not m.drives() and "can't drive" in st["log"][-1]


def test_gemini_function_calls_with_fake_client(monkeypatch):
    pytest.importorskip("google.genai")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    m = FakeMission()
    cmd = gc.Commander(m, {"mock": False, "model": "gemini-test"})
    assert not cmd.use_rules
    fcs = [pytypes.SimpleNamespace(name="turn", args={"direction": "right", "degrees": 90}),
           pytypes.SimpleNamespace(name="rm_rf", args={}),
           pytypes.SimpleNamespace(name="drive", args={"direction": "forward", "distance_cm": 500}),
           pytypes.SimpleNamespace(name="scan", args={})]
    fake = _FakeModels(pytypes.SimpleNamespace(function_calls=fcs, text=None))
    cmd.client = pytypes.SimpleNamespace(models=fake)
    p = cmd.plan("check the plant on the right")
    assert p["source"] == "gemini" and [a["action"] for a in p["actions"]] == ["turn", "drive", "scan"]
    assert p["actions"][1]["distance_cm"] <= 40 and any("rm_rf" in e for e in p["errors"])
    conf = fake.calls[0]["config"]
    assert conf.tool_config.function_calling_config.mode.value == "ANY"
    assert {f.name for f in conf.tools[0].function_declarations} == gc.ACTIONS
    # Gemini down -> keyword rules, still a plan
    cmd.client = pytypes.SimpleNamespace(models=_FakeModels(ConnectionError("no wifi")))
    p = cmd.plan("scan")
    assert p["source"] == "rules" and p["actions"] == [{"action": "scan"}]


# ---------- routes ----------
def test_routes_mock(tmp_path):
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plantbot.config import Cfg
    from plantbot.store import Store
    from plantbot.web.gemini_routes import install_gemini

    m = FakeMission()
    store = Store(str(tmp_path / "scans"))
    m.r = pytypes.SimpleNamespace(cfg=Cfg(gemini=Cfg(mock="auto"), mock_mode=True), store=store)
    sent = []

    async def bc(msg):
        sent.append(msg)
    m.broadcast = bc
    app = FastAPI()
    doctor, commander = install_gemini(app, m)
    c = TestClient(app)
    assert c.get("/api/gemini/doctor/latest").json()["scan"] is None
    assert c.get("/api/gemini/status").json()["doctor"]["mock"] is True
    r = c.post("/api/gemini/command", json={"text": "how is the basil doing?", "execute": False}).json()
    assert r["actions"][0]["action"] == "say"
    assert c.post("/api/gemini/command", json={}).status_code == 400
    assert c.get("/gemini").status_code == 200
