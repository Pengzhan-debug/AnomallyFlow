"""Versioned visual-service contract, independent of the investigation model.

HTTP is opt-in. Missing configuration and invalid responses never fall back to Mock.
"""
from __future__ import annotations

import base64
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .db import AuditRow


class DetectorConfigError(ValueError):
    pass


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    model_version: str = Field(min_length=1, max_length=80)
    threshold_version: str = Field(min_length=1, max_length=80)
    normal_max: float = Field(ge=0, le=1)
    abnormal_min: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.normal_max >= self.abnormal_min:
            raise ValueError("Threshold band must be ordered")
        return self


class ServiceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    request_id: str
    product_code: str
    image_sha256: str
    status: Literal["NORMAL", "ABNORMAL", "UNCERTAIN", "ERROR"]
    anomaly_score: float | None = Field(ge=0, le=1)
    quality_passed: bool
    confidence_note: str = Field(max_length=500)
    model_version: str = Field(min_length=1, max_length=80)
    threshold_version: str = Field(min_length=1, max_length=80)
    inference_ms: float = Field(ge=0, le=120000)
    defect_label: str | None = Field(default=None, max_length=80)
    anomaly_map_png_base64: str | None = Field(default=None, max_length=6_000_000)


def detector_config(product_code: str, mode: str | None = None) -> dict:
    mode = mode or os.environ.get("DETECTOR_MODE", "mock")
    if mode == "mock":
        return {"mode": "mock", "model_version": "mock-detector-v1",
                "threshold_version": f"{product_code}-v1", "normal_max": 0.35, "abnormal_min": 0.8}
    if mode != "http":
        raise DetectorConfigError("DETECTOR_MODE must be mock or http")
    endpoint = os.environ.get("DETECTOR_URL", "")
    try:
        parsed = urlsplit(endpoint)
        parsed.port  # reject invalid port syntax before a task is enqueued
    except ValueError:
        raise DetectorConfigError("Invalid detector URL") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise DetectorConfigError("Set a detector HTTP URL without embedded credentials")
    try:
        profiles = json.loads(Path(os.environ["DETECTOR_PROFILES_PATH"]).read_text(encoding="utf-8"))
        profile = Profile.model_validate(profiles[product_code])
    except (KeyError, OSError, ValueError, ValidationError):
        raise DetectorConfigError("No valid versioned profile for this product") from None
    return {"mode": "http", "endpoint": endpoint, **profile.model_dump()}


def failure(config, code):
    return {"status": "ERROR", "anomaly_score": None, "quality_passed": False,
            "confidence_note": "检测未完成，请人工检查图像和检测服务。", "defect_label": None,
            "model_version": config["model_version"], "threshold_version": config["threshold_version"],
            "inference_ms": None, "anomaly_map_key": None, "error_code": code,
            "mode": config["mode"]}


def inspect(task, config, storage_root: Path, *, transport=None) -> dict:
    if config["mode"] == "mock":
        scenario = config.get("scenario_id")
        if scenario == "SC-05":
            return failure(config, "MOCK_DETECTOR_ERROR")
        status, score = ("NORMAL", 0.18) if scenario == "SC-01" else ("UNCERTAIN", 0.71) if scenario == "SC-04" else ("ABNORMAL", 0.87)
        return {"status": status, "anomaly_score": score, "quality_passed": status != "UNCERTAIN",
                "confidence_note": "固定合成场景，仅用于流程联调，未分析图像像素。", "defect_label": None,
                "model_version": config["model_version"], "threshold_version": config["threshold_version"],
                "inference_ms": 0, "anomaly_map_key": None, "error_code": None, "mode": "mock"}
    root = storage_root.resolve()
    target = (root / task.image_key).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return failure(config, "DETECTOR_IMAGE_UNAVAILABLE")
    image = target.read_bytes()
    if sha256(image).hexdigest() != task.image_sha256:
        return failure(config, "DETECTOR_IMAGE_HASH_MISMATCH")
    auth = os.environ.get("DETECTOR_API_KEY", "")
    headers = {"Idempotency-Key": f"{task.id}:{config['model_version']}:{config['threshold_version']}"}
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    try:
        with httpx.Client(timeout=20, follow_redirects=False, transport=transport) as client:
            with client.stream("POST", config["endpoint"], headers=headers, data={
                "request_id": task.id, "tenant_id": task.tenant_id, "product_code": task.product_code,
                "image_sha256": task.image_sha256, "model_version": config["model_version"],
                "threshold_version": config["threshold_version"]},
                files={"image": ("inspection.png" if task.image_mime == "image/png" else "inspection.jpg", image, task.image_mime)}) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 8_000_000:
                        return failure(config, "DETECTOR_RESPONSE_INVALID")
                    chunks.append(chunk)
        result = ServiceResult.model_validate_json(b"".join(chunks))
        if (result.request_id != task.id or result.product_code != task.product_code
                or result.image_sha256 != task.image_sha256 or result.model_version != config["model_version"]
                or result.threshold_version != config["threshold_version"]):
            return failure(config, "DETECTOR_VERSION_MISMATCH")
        if result.status == "ERROR":
            return failure(config, "DETECTOR_SERVICE_ERROR")
        if result.anomaly_score is None:
            return failure(config, "DETECTOR_RESPONSE_INVALID")
        # A service cannot relax the locally pinned quality gate or thresholds.
        expected = "NORMAL" if result.anomaly_score <= config["normal_max"] else "ABNORMAL" if result.anomaly_score >= config["abnormal_min"] else "UNCERTAIN"
        status = "UNCERTAIN" if not result.quality_passed or result.status == "UNCERTAIN" else expected
        if result.status != expected and status != "UNCERTAIN":
            return failure(config, "DETECTOR_THRESHOLD_MISMATCH")
        heatmap = None
        if result.anomaly_map_png_base64:
            raw = base64.b64decode(result.anomaly_map_png_base64, validate=True)
            if len(raw) > 4_000_000:
                raise ValueError("Heatmap too large")
            with Image.open(BytesIO(raw)) as opened:
                if opened.format != "PNG" or opened.size != (task.image_width, task.image_height):
                    raise ValueError("Heatmap must align to original pixels")
                opened.verify()
            with Image.open(BytesIO(raw)) as opened:
                opened.load()
            heatmap = f"findings/{task.id}/anomaly.png"
            heatmap_path = root / heatmap
            heatmap_path.parent.mkdir(parents=True, exist_ok=True)
            heatmap_path.write_bytes(raw)
        return {**result.model_dump(exclude={"request_id", "product_code", "image_sha256", "anomaly_map_png_base64"}),
                "status": status, "anomaly_map_key": heatmap, "error_code": None, "mode": "http"}
    except httpx.TimeoutException:
        return failure(config, "DETECTOR_TIMEOUT")
    except httpx.HTTPError:
        return failure(config, "DETECTOR_UNAVAILABLE")
    except (ValueError, OSError, Image.DecompressionBombError):
        return failure(config, "DETECTOR_RESPONSE_INVALID")


def apply_detection(db, task, batch, *, transport=None):
    if task.detection_json is None:
        # Old queued tasks retain their original deterministic detector.
        config = task.detector_config or detector_config(task.product_code, "mock")
        config = {**config, "scenario_id": batch.scenario_id}
        result = inspect(task, config, Path(os.environ.get("STORAGE_ROOT", "./storage")), transport=transport)
        task.detection_json = result
        task.detection_status, task.anomaly_score = result["status"], result["anomaly_score"]
        task.model_version, task.threshold_version = result["model_version"], result["threshold_version"]
        task.error_code = result["error_code"]
        db.add(AuditRow(id=str(uuid4()), tenant_id=task.tenant_id, actor_id=None,
            action="DETECTION_COMPLETED", resource_type="inspection", resource_id=task.id,
            payload={"mode": result["mode"], "status": result["status"], "model_version": task.model_version,
                     "threshold_version": task.threshold_version, "error_code": task.error_code}))
    task.status = {"NORMAL": "COMPLETED", "ERROR": "NEEDS_REVIEW"}.get(task.detection_status, "INVESTIGATING")
    return task.detection_json
