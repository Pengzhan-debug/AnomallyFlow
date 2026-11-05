"""Anomalib-backed visual detection service for AnomallyFlow.

Implements ``design/视觉检测接入.md``: strict JSON, server-side status derived
from the pinned threshold band, anomaly map aligned to the original image, and
idempotent replay. AnomallyFlow still re-validates every field and threshold.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from hashlib import sha256
import hmac
from io import BytesIO
import logging
import threading
import time

import numpy as np
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from .model_store import ModelStore, ModelUnavailable
from .runners import build_runner
from .schemas import InspectResponse
from .settings import Settings

logger = logging.getLogger("detector")

ALLOWED_FORMATS = {"PNG", "JPEG"}
MAX_HEATMAP_BYTES = 4_000_000
MAX_HEATMAP_B64 = 6_000_000


class ResponseCache:
    """Idempotent replay for repeated ``Idempotency-Key`` requests."""

    def __init__(self, ttl_seconds: int, max_entries: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._items: OrderedDict[str, tuple[float, tuple, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str, fingerprint: tuple) -> dict | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            stored_at, stored_fingerprint, payload = item
            if time.monotonic() - stored_at > self.ttl_seconds:
                del self._items[key]
                return None
            if stored_fingerprint != fingerprint:
                raise HTTPException(status_code=409, detail="Idempotency key reused with different request")
            self._items.move_to_end(key)
            return dict(payload)

    def put(self, key: str, fingerprint: tuple, payload: dict) -> None:
        with self._lock:
            self._items[key] = (time.monotonic(), fingerprint, dict(payload))
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)


def _elapsed_ms(started: float) -> float:
    return min(120000.0, round((time.perf_counter() - started) * 1000, 3))


def _status_for(score: float, normal_max: float, abnormal_min: float) -> str:
    if score <= normal_max:
        return "NORMAL"
    if score >= abnormal_min:
        return "ABNORMAL"
    return "UNCERTAIN"


def _encode_heatmap(anomaly_map: np.ndarray | None, size: tuple[int, int]) -> str | None:
    if anomaly_map is None:
        return None
    array = np.nan_to_num(np.asarray(anomaly_map, dtype=np.float32), nan=0.0, posinf=1.0, neginf=0.0)
    low, high = float(array.min()), float(array.max())
    array = (array - low) / (high - low) if high > low else np.zeros_like(array)
    pixels = Image.fromarray((array * 255).clip(0, 255).astype(np.uint8), mode="L")
    pixels = pixels.resize(size, Image.Resampling.BILINEAR)
    buffer = BytesIO()
    pixels.save(buffer, format="PNG", optimize=True)
    raw = buffer.getvalue()
    if len(raw) > MAX_HEATMAP_BYTES:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return encoded if len(encoded) <= MAX_HEATMAP_B64 else None


def create_app(settings: Settings | None = None, runner_builder=build_runner) -> FastAPI:
    settings = settings or Settings.from_env()
    store = ModelStore(settings, runner_builder)
    cache = ResponseCache(settings.cache_ttl_seconds, settings.cache_max_entries)

    app = FastAPI(title="AnomallyFlow anomalib detector", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store

    def authorize(authorization: str | None = Header(default=None)) -> None:
        if not settings.api_key:
            return
        expected = f"Bearer {settings.api_key}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "engine": "stub" if settings.stub else "anomalib",
                "deployed_products": store.deployed_products()}

    @app.post("/inspect", response_model=InspectResponse, response_model_exclude_none=False)
    def inspect(
        request_id: str = Form(..., min_length=1, max_length=128),
        tenant_id: str = Form(..., min_length=1, max_length=128),
        product_code: str = Form(..., min_length=1, max_length=64),
        image_sha256: str = Form(..., pattern=r"^[0-9a-f]{64}$"),
        model_version: str = Form(..., min_length=1, max_length=80),
        threshold_version: str = Form(..., min_length=1, max_length=80),
        image: UploadFile = File(...),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        authorization: str | None = Header(default=None),
    ) -> dict:
        authorize(authorization)
        if not idempotency_key:
            raise HTTPException(status_code=400, detail="Idempotency-Key is required")
        if len(idempotency_key) > 200:
            raise HTTPException(status_code=400, detail="Idempotency-Key is too long")

        data = image.file.read(settings.max_image_bytes + 1)
        if len(data) > settings.max_image_bytes:
            raise HTTPException(status_code=413, detail="Image exceeds size limit")
        digest = sha256(data).hexdigest()
        if digest != image_sha256:
            return _error(request_id, product_code, image_sha256, model_version, threshold_version,
                          "上传图像与声明的 SHA256 不一致。", 0.0)

        fingerprint = (request_id, product_code, image_sha256, model_version, threshold_version)
        cached = cache.get(idempotency_key, fingerprint)
        if cached is not None:
            return cached

        started = time.perf_counter()
        try:
            profile = store.profile(product_code)
        except ModelUnavailable:
            return _finish_error(request_id, product_code, image_sha256,
                                 model_version, threshold_version, "该产品没有有效的检测配置。", started)

        if model_version != profile.model_version or threshold_version != profile.threshold_version:
            return _finish_error(request_id, product_code, image_sha256,
                                 model_version, threshold_version, "请求的模型或阈值版本与部署配置不一致。", started)

        try:
            opened = Image.open(BytesIO(data))
            if opened.format not in ALLOWED_FORMATS:
                raise UnidentifiedImageError("unsupported format")
            opened.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            return _finish_error(request_id, product_code, image_sha256,
                                 model_version, threshold_version, "图像无法解码，请人工检查文件。", started)

        width, height = opened.size
        gray = ImageOps.grayscale(opened)
        _low, high = gray.getextrema()
        quality_passed = width >= 32 and height >= 32 and (high - _low) >= 8
        reasons = []
        if width < 32 or height < 32:
            reasons.append("分辨率低于 32 像素")
        if (high - _low) < 8:
            reasons.append("图像近似空白")

        try:
            runner = store.runner(product_code, model_version)
            prediction = runner.predict(opened.convert("RGB"))
            score = float(prediction.score)
            if not np.isfinite(score):
                raise ValueError("non-finite score")
            score = min(1.0, max(0.0, score))
        except ModelUnavailable:
            return _finish_error(request_id, product_code, image_sha256,
                                 model_version, threshold_version, "该产品尚未部署检测模型。", started)
        except Exception:
            logger.exception("inference failed for task %s", request_id)
            return _finish_error(request_id, product_code, image_sha256,
                                 model_version, threshold_version, "模型推理失败，请人工检查图像和检测服务。", started)

        status = _status_for(score, profile.normal_max, profile.abnormal_min)
        if not quality_passed:
            status = "UNCERTAIN"
        heatmap = _encode_heatmap(prediction.anomaly_map, (width, height))
        note = (f"PatchCore 图像级异常分数（训练集 min-max 归一化），阈值带 [{profile.normal_max:.3f}, "
                f"{profile.abnormal_min:.3f}]；quality_passed 仅表示图像可解码、尺寸足够且非空白，"
                "未进行拍摄质量模型评估。")
        if reasons:
            note = f"拍摄质量未通过：{'、'.join(reasons)}。" + note
        payload = InspectResponse(
            request_id=request_id, product_code=product_code, image_sha256=image_sha256,
            status=status, anomaly_score=score, quality_passed=quality_passed,
            confidence_note=note[:500], model_version=model_version, threshold_version=threshold_version,
            inference_ms=_elapsed_ms(started),
            defect_label=None, anomaly_map_png_base64=heatmap,
        ).model_dump()
        cache.put(idempotency_key, fingerprint, payload)
        return payload

    return app


def _error(request_id, product_code, image_sha256, model_version, threshold_version,
           note: str, elapsed: float) -> dict:
    return InspectResponse(
        request_id=request_id, product_code=product_code, image_sha256=image_sha256,
        status="ERROR", anomaly_score=None, quality_passed=False, confidence_note=note[:500],
        model_version=model_version, threshold_version=threshold_version,
        inference_ms=min(120000.0, round(elapsed * 1000, 3)), defect_label=None, anomaly_map_png_base64=None,
    ).model_dump()


def _finish_error(request_id, product_code, image_sha256,
                  model_version, threshold_version, note, started) -> dict:
    # Errors are not cached: a transient model/OOM failure must be retryable.
    return _error(request_id, product_code, image_sha256, model_version, threshold_version,
                  note, time.perf_counter() - started)


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080, workers=1)
