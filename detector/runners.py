"""Model runners behind the ``/inspect`` contract.

``AnomalibRunner`` is the only place that imports ``anomalib``/``torch`` so the
FastAPI layer, cache and tests stay importable without the ML stack.
"""
from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import threading
from typing import NamedTuple, Protocol

import numpy as np
from PIL import Image


class Prediction(NamedTuple):
    score: float
    anomaly_map: np.ndarray | None


class Runner(Protocol):
    def predict(self, image: Image.Image) -> Prediction: ...


class AnomalibRunner:
    """Load one anomalib torch export and score one image at a time.

    The checkpoint is produced by ``train_patchcore.py`` (``weights/torch/model.pt``
    with a ``{"model": module}`` payload). Loading uses pickle, so the checkpoint
    must be trained by this project and pinned to the same anomalib version.
    """

    def __init__(self, checkpoint: Path, device: str = "cpu") -> None:
        os.environ.setdefault("TRUST_REMOTE_CODE", "1")
        from anomalib.deploy import TorchInferencer

        self.device = device
        self._inferencer = TorchInferencer(path=checkpoint, device=device)
        self._lock = threading.Lock()

    def predict(self, image: Image.Image) -> Prediction:
        with self._lock:
            batch = self._inferencer.predict(image)
        score = float(batch.pred_score.detach().reshape(-1)[0].cpu())
        anomaly_map = batch.anomaly_map
        if anomaly_map is None:
            return Prediction(score, None)
        array = anomaly_map.detach().float().cpu().numpy()
        if array.ndim == 3:
            array = array[0]
        return Prediction(score, array)


class StubRunner:
    """Deterministic placeholder for contract tests; it never reads pixels."""

    def __init__(self, product_code: str) -> None:
        self.product_code = product_code

    def predict(self, image: Image.Image) -> Prediction:
        payload = f"{self.product_code}:{image.size}".encode() + image.tobytes()[:64]
        digest = sha256(payload).digest()
        return Prediction(int.from_bytes(digest[:4], "big") / 0xFFFFFFFF, None)


def build_runner(checkpoint: Path, device: str, product_code: str, stub: bool = False) -> Runner:
    if stub:
        return StubRunner(product_code)
    return AnomalibRunner(checkpoint, device)
