"""Load config.yaml into attribute-access objects.

Plain nested namespaces instead of one dataclass per section: the pin map is still
changing hourly, and a new key in the YAML should just work without a code change.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config.yaml"


class Cfg(SimpleNamespace):
    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def to_dict(self) -> dict:
        return {k: v.to_dict() if isinstance(v, Cfg) else v for k, v in vars(self).items()}


def _wrap(obj: Any) -> Any:
    if isinstance(obj, dict):
        return Cfg(**{k: _wrap(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_wrap(v) for v in obj]
    return obj


def load(path: str | Path | None = None, mock: bool = False, camera: str | None = None) -> Cfg:
    path = Path(path) if path else DEFAULT_PATH
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    cfg = _wrap(raw)
    cfg.path = str(path)
    cfg.mock_mode = mock
    if mock:
        cfg.motors.driver = "mock"
        cfg.env.driver = "mock"
        cfg.ranger.driver = "mock"
        cfg.fan.driver = "mock"
        if cfg.get("tactile") is not None:
            cfg.tactile.driver = "mock"
        cfg.camera.driver = cfg.mock.camera
        cfg.sensing.warmup_s = cfg.mock.warmup_s
        cfg.sensing.warmup_min_s = min(cfg.sensing.warmup_min_s, cfg.mock.warmup_s)
    if camera:
        cfg.camera.driver = camera
    # Relative paths are relative to the repo root, so systemd/cwd don't matter.
    for section, key in (("store", "dir"), ("classifier", "model"), ("classifier", "labels"),
                         ("mock", "camera_folder"), ("sensing.differential", "calibration"),
                         ("tactile.turgor", "calibration")):
        sec = cfg
        for part in section.split("."):
            sec = sec.get(part)
        if sec is None:
            continue
        p = Path(getattr(sec, key))
        setattr(sec, key, str(p if p.is_absolute() else ROOT / p))
    return cfg
