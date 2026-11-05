"""Contract tests for the detector service; no anomalib/torch needed."""
from __future__ import annotations

import base64
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from detector.app import create_app
from detector.runners import Prediction
from detector.settings import Settings

PRODUCT = "VALVE-A"
PROFILE = {"model_version": "visual-model-v1", "threshold_version": "valve-threshold-v1",
           "normal_max": 0.3, "abnormal_min": 0.8}
EXPECTED_KEYS = {"request_id", "product_code", "image_sha256", "status", "anomaly_score",
                 "quality_passed", "confidence_note", "model_version", "threshold_version",
                 "inference_ms", "defect_label", "anomaly_map_png_base64"}


class FakeRunner:
    def __init__(self, score: float, anomaly_map=None, error: Exception | None = None) -> None:
        self.score = score
        self.anomaly_map = anomaly_map
        self.error = error

    def predict(self, image):
        if self.error:
            raise self.error
        return Prediction(self.score, self.anomaly_map)


def make_image(size=(48, 40)) -> tuple[bytes, str]:
    width, height = size
    gradient = np.add.outer(np.arange(height), np.arange(width)) % 256
    pixels = np.stack([gradient, gradient, gradient], axis=-1).astype(np.uint8)
    buffer = BytesIO()
    Image.fromarray(pixels, mode="RGB").save(buffer, format="PNG")
    data = buffer.getvalue()
    return data, sha256(data).hexdigest()


def make_flat_image(size=(48, 40), color=128) -> tuple[bytes, str]:
    buffer = BytesIO()
    Image.new("RGB", size, (color, color, color)).save(buffer, format="PNG")
    data = buffer.getvalue()
    return data, sha256(data).hexdigest()


def build_client(tmp_path: Path, runner: FakeRunner, api_key: str = "") -> TestClient:
    checkpoint = tmp_path / "models" / PRODUCT / "checkpoint.pt"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"fake checkpoint")
    registry = {"schema_version": 1, "products": {PRODUCT: {
        "checkpoint": f"{PRODUCT}/checkpoint.pt",
        "sha256": sha256(b"fake checkpoint").hexdigest()}}}
    (tmp_path / "models" / "registry.json").write_text(json.dumps(registry))
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({PRODUCT: PROFILE}))
    settings = Settings(profiles_path=profiles, registry_path=tmp_path / "models" / "registry.json",
                        api_key=api_key)
    app = create_app(settings, runner_builder=lambda *args, **kwargs: runner)
    return TestClient(app)


def post(client: TestClient, data: bytes, digest: str, *, key: str = "task-1:v1:t1", **overrides):
    fields = {"request_id": "QI-1", "tenant_id": "demo", "product_code": PRODUCT,
              "image_sha256": digest, "model_version": PROFILE["model_version"],
              "threshold_version": PROFILE["threshold_version"], **overrides}
    return client.post("/inspect", data=fields, files={"image": ("inspection.png", data, "image/png")},
                       headers={"Idempotency-Key": key})


@pytest.mark.parametrize("score,status", [(0.2, "NORMAL"), (0.5, "UNCERTAIN"), (0.9, "ABNORMAL")])
def test_status_band_and_contract_shape(tmp_path, score, status):
    image, digest = make_image()
    response = post(build_client(tmp_path, FakeRunner(score)), image, digest)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == EXPECTED_KEYS
    assert body["status"] == status and body["anomaly_score"] == score
    assert body["quality_passed"] is True
    assert body["request_id"] == "QI-1" and body["image_sha256"] == digest


def test_blank_image_fails_quality_and_is_uncertain(tmp_path):
    image, digest = make_flat_image()
    body = post(build_client(tmp_path, FakeRunner(0.1)), image, digest).json()
    assert body["quality_passed"] is False
    assert body["status"] == "UNCERTAIN"
    assert "拍摄质量" in body["confidence_note"]


def test_anomaly_map_matches_original_pixels(tmp_path):
    image, digest = make_image(size=(48, 40))
    heatmap = np.linspace(0, 1, 12).reshape(3, 4)
    body = post(build_client(tmp_path, FakeRunner(0.9, heatmap)), image, digest).json()
    raw = base64.b64decode(body["anomaly_map_png_base64"], validate=True)
    with Image.open(BytesIO(raw)) as opened:
        assert opened.format == "PNG" and opened.size == (48, 40)


def test_heatmap_omitted_when_runner_returns_none(tmp_path):
    image, digest = make_image()
    body = post(build_client(tmp_path, FakeRunner(0.9)), image, digest).json()
    assert body["anomaly_map_png_base64"] is None


def test_idempotent_replay_and_conflicting_reuse(tmp_path):
    image, digest = make_image()
    client = build_client(tmp_path, FakeRunner(0.9))
    first = post(client, image, digest).json()
    assert post(client, image, digest).json() == first
    conflict = post(client, image, digest, product_code=PRODUCT, request_id="QI-2")
    assert conflict.status_code == 409


def test_hash_mismatch_is_error(tmp_path):
    image, _ = make_image()
    client = build_client(tmp_path, FakeRunner(0.9))
    body = post(client, image, "0" * 64).json()
    assert body["status"] == "ERROR" and body["anomaly_score"] is None
    assert body["quality_passed"] is False


def test_unknown_product_is_error(tmp_path):
    image, digest = make_image()
    body = post(build_client(tmp_path, FakeRunner(0.9)), image, digest, product_code="UNKNOWN").json()
    assert body["status"] == "ERROR"
    assert body["model_version"] == PROFILE["model_version"]


def test_version_mismatch_is_error(tmp_path):
    image, digest = make_image()
    body = post(build_client(tmp_path, FakeRunner(0.9)), image, digest, model_version="other").json()
    assert body["status"] == "ERROR"
    assert "版本" in body["confidence_note"]


def test_inference_failure_is_error(tmp_path):
    image, digest = make_image()
    body = post(build_client(tmp_path, FakeRunner(0.0, error=RuntimeError("boom"))), image, digest).json()
    assert body["status"] == "ERROR" and body["anomaly_score"] is None


def test_transient_error_is_retryable(tmp_path):
    class Flaky:
        calls = 0

        def predict(self, image):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("out of memory")
            return Prediction(0.9, None)

    image, digest = make_image()
    client = build_client(tmp_path, Flaky())
    assert post(client, image, digest).json()["status"] == "ERROR"
    assert post(client, image, digest).json()["status"] == "ABNORMAL"


def test_bearer_token_required_when_configured(tmp_path):
    image, digest = make_image()
    client = build_client(tmp_path, FakeRunner(0.9), api_key="secret")
    assert post(client, image, digest).status_code == 401
    response = client.post(
        "/inspect",
        data={"request_id": "QI-1", "tenant_id": "demo", "product_code": PRODUCT,
              "image_sha256": digest, "model_version": PROFILE["model_version"],
              "threshold_version": PROFILE["threshold_version"]},
        files={"image": ("inspection.png", image, "image/png")},
        headers={"Idempotency-Key": "task-1:v1:t1", "Authorization": "Bearer secret"},
    )
    assert response.status_code == 200


def test_missing_model_file_is_error(tmp_path):
    image, digest = make_image()
    client = build_client(tmp_path, FakeRunner(0.9))
    (tmp_path / "models" / PRODUCT / "checkpoint.pt").unlink()
    body = post(client, image, digest).json()
    assert body["status"] == "ERROR"
    assert "部署" in body["confidence_note"]
