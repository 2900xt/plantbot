"""Plant health classifier backends. All share one interface: predict(crop_rgb) -> Prediction.

Run a remote classifier on a laptop:  python -m plantbot.vision.classify --serve --backend onnx
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Prediction:
    label: str  # e.g. "healthy", "leaf_spot", "blight"
    p_healthy: float  # 0..1, what fusion uses
    probs: dict[str, float]
    backend: str  # "tflite-pi" | "onnx-pi" | "remote-laptop" | "dummy"
    ms: float

    def to_dict(self) -> dict:
        return asdict(self)


class Classifier(Protocol):
    def predict(self, crop_rgb: np.ndarray) -> Prediction: ...


def _load_labels(path: str) -> list[str]:
    labels = [l.strip() for l in Path(path).read_text().splitlines() if l.strip()]
    if "healthy" not in labels:
        raise ValueError(f"{path} must contain a 'healthy' label")
    return labels


def _softmax(x: np.ndarray) -> np.ndarray:
    if np.all(x >= 0) and abs(float(x.sum()) - 1) < 1e-3:
        return x  # already probabilities
    e = np.exp(x - x.max())
    return e / e.sum()


def _preprocess(crop: np.ndarray, size: int, norm: str) -> np.ndarray:
    x = cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32)
    if norm == "0-1":
        x /= 255.0
    elif norm == "-1-1":
        x = x / 127.5 - 1.0
    elif norm == "imagenet":
        x = (x / 255.0 - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
    return x  # HWC RGB


def _to_prediction(probs: np.ndarray, labels: list[str], backend: str, t0: float) -> Prediction:
    probs = _softmax(probs.astype(np.float64).ravel())
    pd = {l: float(p) for l, p in zip(labels, probs)}
    label = max(pd, key=pd.get)
    return Prediction(label, pd.get("healthy", 0.0), pd, backend, (time.perf_counter() - t0) * 1000)


class DummyClassifier:
    """Stand-in until the real model lands: scores by the share of yellow/brown vs green.

    It's a heuristic, not ML, but it responds to the image so the whole pipeline is testable.
    """

    def __init__(self, cfg=None):
        self.labels = ["healthy", "leaf_spot", "blight"]

    def predict(self, crop_rgb: np.ndarray) -> Prediction:
        t0 = time.perf_counter()
        hsv = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2HSV)
        h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        plant = (s > 40) & (v > 30) & (h >= 5) & (h <= 90) & ((h >= 22) | (s >= 70))
        bad = plant & (h < 30)  # brown + yellow
        frac = float(bad.sum()) / max(1, int(plant.sum()))
        p_h = float(np.clip(1.0 - frac * 4.0, 0.02, 0.98))
        sick = 1 - p_h
        spot_share = 0.7 if frac < 0.15 else 0.35
        probs = np.array([p_h, sick * spot_share, sick * (1 - spot_share)])
        return _to_prediction(probs, self.labels, "dummy", t0)


class TFLiteClassifier:
    def __init__(self, cfg):
        try:
            from ai_edge_litert.interpreter import Interpreter  # lazy
        except ImportError:
            try:
                from tflite_runtime.interpreter import Interpreter
            except ImportError:
                from tensorflow.lite import Interpreter  # type: ignore
        self.cfg = cfg
        self.labels = _load_labels(cfg.labels)
        self.it = Interpreter(model_path=cfg.model, num_threads=4)
        self.it.allocate_tensors()
        self.inp = self.it.get_input_details()[0]
        self.out = self.it.get_output_details()[0]

    def predict(self, crop_rgb: np.ndarray) -> Prediction:
        t0 = time.perf_counter()
        x = _preprocess(crop_rgb, self.cfg.input_size, self.cfg.normalize)[None]
        if self.inp["dtype"] == np.uint8:  # quantized model takes raw 0-255
            x = cv2.resize(crop_rgb, (self.cfg.input_size,) * 2)[None].astype(np.uint8)
        self.it.set_tensor(self.inp["index"], x.astype(self.inp["dtype"]))
        self.it.invoke()
        y = self.it.get_tensor(self.out["index"])[0].astype(np.float32)
        scale, zero = self.out.get("quantization", (0, 0))
        if scale:
            y = (y - zero) * scale
        return _to_prediction(y, self.labels, "tflite-pi", t0)


class OnnxClassifier:
    def __init__(self, cfg):
        import onnxruntime as ort  # lazy

        self.cfg = cfg
        self.labels = _load_labels(cfg.labels)
        self.sess = ort.InferenceSession(cfg.model, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0]
        self.nchw = len(self.inp.shape) == 4 and self.inp.shape[1] == 3

    def predict(self, crop_rgb: np.ndarray) -> Prediction:
        t0 = time.perf_counter()
        x = _preprocess(crop_rgb, self.cfg.input_size, self.cfg.normalize)
        if self.nchw:
            x = x.transpose(2, 0, 1)
        y = self.sess.run(None, {self.inp.name: x[None].astype(np.float32)})[0][0]
        return _to_prediction(y, self.labels, "onnx-pi", t0)


class RemoteClassifier:
    """POSTs the crop as JPEG to a laptop running `python -m plantbot.vision.classify --serve`."""

    def __init__(self, cfg):
        self.url, self.timeout = cfg.remote_url, cfg.remote_timeout_s

    def predict(self, crop_rgb: np.ndarray) -> Prediction:
        import json
        import urllib.request

        t0 = time.perf_counter()
        ok, jpg = cv2.imencode(".jpg", cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2BGR))
        req = urllib.request.Request(self.url, data=jpg.tobytes(), headers={"Content-Type": "image/jpeg"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read())
        return Prediction(d["label"], d["p_healthy"], d["probs"], "remote-" + d.get("backend", "laptop"),
                          (time.perf_counter() - t0) * 1000)


BACKENDS = {"dummy": DummyClassifier, "tflite": TFLiteClassifier, "onnx": OnnxClassifier, "remote": RemoteClassifier}


def make(cfg) -> tuple[Classifier, str]:
    """Returns (classifier, status). Falls back to dummy if the backend can't load."""
    try:
        c = BACKENDS[cfg.backend](cfg)
        return c, "ok" if cfg.backend != "dummy" else "dummy"
    except Exception as e:
        log.error("classifier backend %r failed to load: %s; using dummy", cfg.backend, e)
        return DummyClassifier(), "fallback-dummy"


def serve(cfg, host: str = "0.0.0.0", port: int = 8001) -> None:
    import uvicorn
    from fastapi import FastAPI, Request

    clf, status = make(cfg)
    app = FastAPI()

    @app.post("/predict")
    async def predict(req: Request):
        buf = np.frombuffer(await req.body(), np.uint8)
        rgb = cv2.cvtColor(cv2.imdecode(buf, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
        return clf.predict(rgb).to_dict()

    log.info("remote classifier (%s, %s) on %s:%d", cfg.backend, status, host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import argparse

    from plantbot import config

    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--backend")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--config")
    ap.add_argument("image", nargs="?")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    c = config.load(a.config).classifier
    if a.backend:
        c.backend = a.backend
    if a.serve:
        serve(c, port=a.port)
    elif a.image:
        img = cv2.cvtColor(cv2.imread(a.image), cv2.COLOR_BGR2RGB)
        print(make(c)[0].predict(img))
