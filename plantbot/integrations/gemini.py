"""Plant Doctor: a second opinion from Gemini on each scan.

Gemini gets the plant crop (JPEG), what our CNN said, and the VOC/environment readings from the
sniff, and returns a structured diagnosis (JSON schema enforced by the API):
{species_guess, health, likely_issue, evidence[], confidence, care_actions[], urgency, explain_for_farmer}.

Never on the motor path: `diagnose()` blocks (up to timeout_s) and is meant for a worker thread;
`diagnose_scan_async()` wraps it in asyncio.to_thread. Results are cached in memory and written to
scans/<id>/gemini.json, so a page reload or a restart doesn't pay for the same scan twice.

Key: GEMINI_API_KEY (or GOOGLE_API_KEY) from the environment. On the Pi it lives in ~/plantbot/.env,
which systemd loads (EnvironmentFile); on a laptop we also read <repo>/.env. No key, no network,
or no SDK = diagnose() returns None and logs once; the rover runs exactly as before.
Mock: `gemini.mock: true` (or `auto` + --mock + no key) returns a canned, plausible diagnosis built
from the CNN label and the VOC ratios, marked source="mock", so the demo works offline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MODEL = "gemini-3.6-flash"  # stable Flash (Nov 2026); gemini-3.5-flash-lite is faster, 3.8-flash smarter
HEALTH = ["healthy", "stressed", "diseased"]
URGENCY = ["none", "low", "medium", "high"]

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "species_guess": {"type": "string", "description": "Common name of the plant, or 'unknown'."},
        "health": {"type": "string", "enum": HEALTH},
        "likely_issue": {"type": "string",
                         "description": "Most likely problem (disease, pest, nutrient, water, heat), or 'none'."},
        "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 5,
                     "description": "Short observations from the photo AND the sensor readings."},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "care_actions": {"type": "array", "items": {"type": "string"}, "maxItems": 4,
                         "description": "Concrete next steps a grower can do today."},
        "urgency": {"type": "string", "enum": URGENCY},
        "explain_for_farmer": {"type": "string",
                               "description": "1-2 plain sentences, no jargon, for a farmer reading on a phone."},
    },
    "required": ["species_guess", "health", "likely_issue", "evidence", "confidence", "care_actions",
                 "urgency", "explain_for_farmer"],
    "propertyOrdering": ["species_guess", "health", "likely_issue", "evidence", "confidence",
                         "care_actions", "urgency", "explain_for_farmer"],
}

SYSTEM = (
    "You are Plant Doctor, an agronomist on a small field rover. You get one close-up photo of a single "
    "plant, a label from the rover's small on-board CNN (often wrong, trained on few classes), and gas "
    "readings sniffed next to the plant. Combine all three. Trust what you see in the photo most; use the "
    "gas readings as supporting evidence (stressed or rotting plants emit more VOCs). If the photo is "
    "unclear, say so in evidence and lower confidence. Never invent lab results. Keep care_actions "
    "practical and cheap. explain_for_farmer: 1-2 short plain sentences."
)


def load_env_key(names: tuple[str, ...] = ("GEMINI_API_KEY", "GOOGLE_API_KEY")) -> str:
    """Key from the environment, else from <repo>/.env (the file systemd loads on the Pi)."""
    for n in names:
        if os.getenv(n):
            return os.environ[n].strip()
    env = ROOT / ".env"
    try:
        for line in env.read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k.strip().removeprefix("export ").strip() in names:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def make_client(key: str, timeout_s: float):
    """google-genai client with a hard per-request timeout and no retries (we'd rather fail fast)."""
    from google import genai  # lazy: the rover runs without the SDK installed
    from google.genai import types

    return genai.Client(api_key=key, http_options=types.HttpOptions(
        timeout=int(timeout_s * 1000), retry_options=types.HttpRetryOptions(attempts=1)))


def _get(cfg, key: str, default: Any = None) -> Any:
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def _num(x, nd: int = 3):
    try:
        return None if x is None else round(float(x), nd)
    except (TypeError, ValueError):
        return None


def sensor_summary(env: dict) -> dict:
    """The sniff readings Gemini sees, with plain-language meaning attached."""
    return {
        "bme680_gas_ohms": _num(env.get("gas_ohms"), 0),
        "bme680_gas_ratio_vs_clean_air": _num(env.get("gas_ratio")),
        "mq2_volts": _num(env.get("mq2_v")),
        "mq2_ratio_vs_clean_air": _num(env.get("mq2_ratio")),
        "plant_minus_background_ratio": _num(env.get("diff_ratio")),
        "temp_c": _num(env.get("temp_c"), 1),
        "humidity_pct": _num(env.get("rh"), 0),
        "how_to_read": "ratios are sensor resistance now / clean-air baseline: 1.0 = clean air, "
                       "lower = more VOCs (e.g. 0.7 = clearly elevated). plant_minus_background < 1 means "
                       "the air at the plant smells more than the air behind the rover.",
    }


def normalize(d: dict) -> dict:
    """Clamp model output to the schema, so the UI never has to defend against it."""
    def strs(v, n):
        return [str(s)[:200] for s in (v if isinstance(v, list) else [v] if v else [])][:n]

    try:
        conf = max(0.0, min(1.0, float(d.get("confidence", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    health = str(d.get("health", "")).lower()
    urgency = str(d.get("urgency", "")).lower()
    return {
        "species_guess": str(d.get("species_guess") or "unknown")[:80],
        "health": health if health in HEALTH else "stressed",
        "likely_issue": str(d.get("likely_issue") or "unknown")[:120],
        "evidence": strs(d.get("evidence"), 5),
        "confidence": round(conf, 2),
        "care_actions": strs(d.get("care_actions"), 4),
        "urgency": urgency if urgency in URGENCY else "medium",
        "explain_for_farmer": str(d.get("explain_for_farmer") or "")[:400],
    }


def mock_diagnosis(label: str, confidence: float, env: dict) -> dict:
    """Canned but consistent with the inputs: a healthy CNN label and clean air give a healthy plant."""
    gas, mq2, diff = (env.get(k) for k in ("gas_ratio", "mq2_ratio", "diff_ratio"))
    smelly = any(r is not None and r < 0.8 for r in (gas, mq2, diff))
    sniff = f"VOC ratio {min(r for r in (gas, mq2, diff) if r is not None):.2f} vs clean air" \
        if any(r is not None for r in (gas, mq2, diff)) else "no gas reading"
    label = (label or "unknown").lower()
    if label == "healthy" and not smelly:
        return normalize({
            "species_guess": "basil", "health": "healthy", "likely_issue": "none",
            "evidence": ["Leaves are evenly green with no spots or curling", f"Air near the plant is clean ({sniff})",
                         f"On-board model agrees: {label} ({confidence:.0%})"],
            "confidence": 0.82, "care_actions": ["Keep the current watering schedule", "Re-scan in 3 days"],
            "urgency": "none",
            "explain_for_farmer": "This plant looks healthy and the air around it smells normal. No action needed.",
        })
    if label == "healthy":
        return normalize({
            "species_guess": "basil", "health": "stressed", "likely_issue": "early stress (water or heat)",
            "evidence": ["Leaves look mostly green", f"But the plant is giving off extra gases ({sniff})",
                         "Smell often changes a day or two before the leaves do"],
            "confidence": 0.6, "care_actions": ["Check soil moisture at 2 cm depth", "Look under leaves for pests",
                                                "Re-scan tomorrow"],
            "urgency": "low",
            "explain_for_farmer": "It looks fine, but it smells stressed. Check the soil and look under the "
                                  "leaves today.",
        })
    issue = "fungal leaf spot" if "spot" in label else "early blight" if "blight" in label else label.replace("_", " ")
    return normalize({
        "species_guess": "basil", "health": "diseased", "likely_issue": issue,
        "evidence": ["Brown and yellow lesions on several leaves", f"Elevated VOCs near the plant ({sniff})" if smelly
                     else f"Air near the plant is close to normal ({sniff})",
                     f"On-board model says {label} ({confidence:.0%})"],
        "confidence": 0.74 if smelly else 0.62,
        "care_actions": ["Remove and bag the spotted leaves", "Water at the base, not on the leaves",
                         "Give it more airflow", "Keep it away from neighbouring plants"],
        "urgency": "high" if smelly else "medium",
        "explain_for_farmer": f"This plant probably has {issue}. Pull off the bad leaves today and keep the "
                              "leaves dry so it doesn't spread.",
    })


class PlantDoctor:
    def __init__(self, cfg=None, mock_mode: bool = False, store_root: str | None = None):
        self.cfg = cfg
        self.enabled = bool(_get(cfg, "enabled", True))
        self.model = _get(cfg, "model", DEFAULT_MODEL)
        self.timeout_s = float(_get(cfg, "timeout_s", 15))
        self.thinking_level = _get(cfg, "thinking_level", "low")  # null = model default
        self.store_root = store_root
        self.key = load_env_key()
        m = _get(cfg, "mock", "auto")
        self.mock = m is True or (m == "auto" and mock_mode and not self.key)
        self.cache: dict[str, dict] = {}
        self.inflight: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.client = None
        self.last_error: str | None = None
        self._warned: set[str] = set()
        if not self.enabled:
            log.info("Plant Doctor disabled (gemini.enabled: false)")
        elif self.mock:
            log.info("Plant Doctor: mock mode (canned diagnoses, no network)")
        elif not self.key:
            self._warn_once("nokey", "Plant Doctor off: set GEMINI_API_KEY (Pi: ~/plantbot/.env)")
        else:
            log.info("Plant Doctor: Gemini %s", self.model)

    @property
    def available(self) -> bool:
        return self.enabled and (self.mock or bool(self.key))

    def status(self) -> dict:
        return {"enabled": self.enabled, "available": self.available, "mock": self.mock, "model": self.model,
                "error": self.last_error, "cached": len(self.cache)}

    def _warn_once(self, tag: str, msg: str) -> None:
        self.last_error = msg
        if tag not in self._warned:
            self._warned.add(tag)
            log.warning("%s", msg)

    # ---------- cache ----------
    def _path(self, scan_id: str) -> str | None:
        if not self.store_root:
            return None
        return os.path.join(self.store_root, os.path.basename(scan_id), "gemini.json")

    def cached(self, scan_id: str) -> dict | None:
        if scan_id in self.cache:
            return self.cache[scan_id]
        p = self._path(scan_id)
        if p and os.path.exists(p):
            try:
                with open(p) as f:
                    self.cache[scan_id] = json.load(f)
                return self.cache[scan_id]
            except (OSError, ValueError):
                pass
        return None

    def _remember(self, scan_id: str, d: dict) -> None:
        self.cache[scan_id] = d
        p = self._path(scan_id)
        if p and os.path.isdir(os.path.dirname(p)):
            try:
                with open(p, "w") as f:
                    json.dump(d, f, indent=2)
            except OSError as e:
                log.warning("Plant Doctor: can't write %s: %s", p, e)

    # ---------- diagnose ----------
    def diagnose(self, scan_id: str, jpeg: bytes | None, label: str, confidence: float, env: dict,
                 refresh: bool = False) -> dict | None:
        """Blocking (<= timeout_s). Call from a worker thread. None if unavailable or failed."""
        if not self.available:
            return None
        if not refresh and (hit := self.cached(scan_id)):
            return hit
        with self.lock:  # one call per scan id: the auto hook and a page load can race
            ev = self.inflight.get(scan_id)
            owner = ev is None
            if owner:
                ev = self.inflight[scan_id] = threading.Event()
        if not owner:
            ev.wait(self.timeout_s + 5)
            return self.cached(scan_id)
        try:
            t0 = time.perf_counter()
            if self.mock:
                d, source = mock_diagnosis(label, confidence, env), "mock"
            else:
                d, source = self._call(jpeg, label, confidence, env), "gemini"
            if d is None:
                return None
            d = {**d, "scan_id": scan_id, "source": source, "model": None if self.mock else self.model,
                 "latency_ms": round((time.perf_counter() - t0) * 1000), "t": time.time(),
                 "cnn": {"label": label, "confidence": _num(confidence)}}
            self._remember(scan_id, d)
            if source == "gemini":
                self.last_error = None
                log.info("Plant Doctor %s: %s / %s (%.0f%%) in %d ms", scan_id, d["health"], d["likely_issue"],
                         100 * d["confidence"], d["latency_ms"])
            return d
        finally:
            with self.lock:
                self.inflight.pop(scan_id, None)
            ev.set()

    def _call(self, jpeg: bytes | None, label: str, confidence: float, env: dict) -> dict | None:
        try:
            from google.genai import types
        except ImportError:
            self._warn_once("sdk", "Plant Doctor off: pip install google-genai")
            return None
        try:
            if self.client is None:
                self.client = make_client(self.key, self.timeout_s)
            facts = {"cnn_label": label, "cnn_confidence": _num(confidence), "sensors": sensor_summary(env)}
            parts = []
            if jpeg:
                parts.append(types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"))
            parts.append("Diagnose this plant. Rover readings:\n" + json.dumps(facts, indent=1))
            kw = dict(system_instruction=SYSTEM, response_mime_type="application/json",
                      response_json_schema=DIAGNOSIS_SCHEMA, temperature=0.2, max_output_tokens=1024)
            if self.thinking_level:
                kw["thinking_config"] = types.ThinkingConfig(thinking_level=str(self.thinking_level).upper())
            resp = self.client.models.generate_content(model=self.model, contents=parts,
                                                       config=types.GenerateContentConfig(**kw))
            data = resp.parsed if isinstance(getattr(resp, "parsed", None), dict) else json.loads(resp.text or "")
            return normalize(data)
        except Exception as e:  # offline, timeout, quota, bad key, bad JSON: all the same to the rover
            msg = f"{type(e).__name__}: {str(e)[:200]}"
            self.last_error = msg
            self._warn_once("call:" + type(e).__name__, f"Plant Doctor call failed ({msg}); will keep trying per scan")
            return None

    # ---------- scan records ----------
    def inputs_from_scan(self, rec: dict) -> tuple[bytes | None, str, float, dict]:
        jpeg = None
        if self.store_root:
            for name in ("crop.jpg", "raw.jpg"):
                p = os.path.join(self.store_root, os.path.basename(rec["id"]), name)
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        jpeg = f.read()
                    break
        probs = rec.get("probs") or {}
        label = rec.get("label") or "unknown"
        conf = probs.get(label, rec.get("p_healthy") if label == "healthy" else None)
        env = {**(rec.get("voc") or {})}
        for k in ("mq2_ratio", "gas_ratio", "diff_ratio", "temp_c", "rh"):
            env.setdefault(k, rec.get(k))
        return jpeg, label, float(conf if conf is not None else 0.5), env

    def diagnose_scan(self, rec: dict, refresh: bool = False) -> dict | None:
        if not self.available:
            return None
        if not refresh and (hit := self.cached(rec["id"])):
            return hit
        return self.diagnose(rec["id"], *self.inputs_from_scan(rec), refresh=refresh)

    async def diagnose_scan_async(self, rec: dict, refresh: bool = False) -> dict | None:
        """Never blocks the event loop; gives up a little after timeout_s even if the SDK hangs."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(self.diagnose_scan, rec, refresh), self.timeout_s + 5)
        except asyncio.TimeoutError:
            self._warn_once("timeout", f"Plant Doctor timed out after {self.timeout_s + 5:.0f} s")
            return None
