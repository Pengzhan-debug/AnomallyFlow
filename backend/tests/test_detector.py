import base64
from hashlib import sha256
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from test_api import TestAPI as Harness, png_bytes
from test_agent import ScriptedModel
import qualityflow.agent_graph as agent_module
import qualityflow.detector as detector
from qualityflow.db import InspectionRow, ReportRow
from qualityflow.worker import run_once, session_factory_for


PROFILE = {"model_version": "visual-model-v1", "threshold_version": "valve-threshold-v1", "normal_max": 0.3, "abnormal_min": 0.8}


@pytest.fixture
def visual(tmp_path, monkeypatch):
    raw = png_bytes()
    (tmp_path / "image.png").write_bytes(raw)
    task = SimpleNamespace(id="QI-test", product_code="VALVE-A", tenant_id="factory", image_key="image.png",
        image_sha256=sha256(raw).hexdigest(), image_mime="image/png", image_width=8, image_height=8)
    config = {"mode": "http", "endpoint": "http://detector.test/inspect", **PROFILE}
    return task, config, tmp_path


def service_payload(task, **overrides):
    return {"request_id": task.id, "product_code": task.product_code, "image_sha256": task.image_sha256,
            "status": "ABNORMAL", "anomaly_score": 0.9, "quality_passed": True,
            "confidence_note": "Synthetic service contract test", "model_version": PROFILE["model_version"],
            "threshold_version": PROFILE["threshold_version"], "inference_ms": 12.0, **overrides}


@pytest.mark.parametrize("status,score,quality,expected", [
    ("NORMAL", 0.3, True, "NORMAL"), ("ABNORMAL", 0.8, True, "ABNORMAL"),
    ("UNCERTAIN", 0.55, True, "UNCERTAIN"), ("NORMAL", 0.1, False, "UNCERTAIN"),
    ("UNCERTAIN", 0.1, True, "UNCERTAIN"), ("NORMAL", 0.9, True, "ERROR")])
def test_http_quality_gate_and_pinned_thresholds(visual, status, score, quality, expected):
    task, config, root = visual
    calls = []
    def handler(request):
        assert request.url.path == "/inspect"
        assert b'name="image"' in request.read()
        assert task.image_sha256.encode() in request.content
        calls.append(request.headers["Idempotency-Key"])
        return httpx.Response(200, json=service_payload(task, status=status, anomaly_score=score, quality_passed=quality))
    result = detector.inspect(task, config, root, transport=httpx.MockTransport(handler))
    assert result["status"] == expected
    assert calls == ["QI-test:visual-model-v1:valve-threshold-v1"]
    if expected == "ERROR":
        assert result["anomaly_score"] is None
        assert result["error_code"] == "DETECTOR_THRESHOLD_MISMATCH"


@pytest.mark.parametrize("fault,code", [
    ("version", "DETECTOR_VERSION_MISMATCH"), ("foreign_image", "DETECTOR_VERSION_MISMATCH"),
    ("timeout", "DETECTOR_TIMEOUT"), ("unavailable", "DETECTOR_UNAVAILABLE"),
    ("invalid_json", "DETECTOR_RESPONSE_INVALID"), ("nan", "DETECTOR_RESPONSE_INVALID"),
    ("missing_score", "DETECTOR_RESPONSE_INVALID"), ("bad_heatmap", "DETECTOR_RESPONSE_INVALID"),
    ("service_error", "DETECTOR_SERVICE_ERROR")])
def test_invalid_or_unavailable_service_never_falls_back_to_normal(visual, fault, code):
    task, config, root = visual
    def handler(request):
        if fault == "timeout": raise httpx.ReadTimeout("secret server detail", request=request)
        if fault == "unavailable": return httpx.Response(503, text="secret server detail")
        if fault == "invalid_json": return httpx.Response(200, text="no json")
        changes = {
            "version": {"threshold_version": "wrong"}, "foreign_image": {"image_sha256": "foreign"},
            "nan": {"anomaly_score": "NaN"}, "missing_score": {"anomaly_score": None},
            "bad_heatmap": {"anomaly_map_png_base64": "not-png"},
            "service_error": {"status": "ERROR", "anomaly_score": None}}[fault]
        return httpx.Response(200, json=service_payload(task, **changes))
    result = detector.inspect(task, config, root, transport=httpx.MockTransport(handler))
    assert result["status"] == "ERROR" and result["anomaly_score"] is None
    assert result["error_code"] == code
    assert "secret" not in json.dumps(result)


def test_heatmap_is_validated_and_original_hash_checked(visual):
    task, config, root = visual
    png = png_bytes("red")
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=service_payload(task, anomaly_map_png_base64=base64.b64encode(png).decode()))
    result = detector.inspect(task, config, root, transport=httpx.MockTransport(handler))
    assert result["status"] == "ABNORMAL"
    assert (root / result["anomaly_map_key"]).read_bytes() == png
    (root / "image.png").write_bytes(png)
    assert detector.inspect(task, config, root, transport=httpx.MockTransport(handler))["error_code"] == "DETECTOR_IMAGE_HASH_MISMATCH"
    assert len(calls) == 1


@pytest.fixture
def app(monkeypatch, tmp_path):
    value = Harness()
    value.setup_method()
    value.app.state.detector_mode = "http"
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({"VALVE-A": PROFILE}), encoding="utf-8")
    monkeypatch.setenv("DETECTOR_URL", "http://detector.test/inspect")
    monkeypatch.setenv("DETECTOR_PROFILES_PATH", str(profiles))
    monkeypatch.setenv("STORAGE_ROOT", str(value.app.state.storage_root))
    yield value
    value.teardown_method()


@pytest.mark.parametrize("mode", ["mock", "llm"])
@pytest.mark.parametrize("status,score,quality,expected", [
    ("NORMAL", 0.1, True, "COMPLETED"), ("ABNORMAL", 0.9, True, "WAITING_APPROVAL"),
    ("UNCERTAIN", 0.5, False, "WAITING_APPROVAL"), ("ERROR", None, False, "NEEDS_REVIEW")])
def test_http_detector_drives_both_investigation_modes_and_survives_restart(app, monkeypatch, mode, status, score, quality, expected):
    app.app.state.agent_mode = mode
    calls = []
    original = detector.inspect
    def inspect(task, config, root, **kwargs):
        calls.append(config.copy())
        return original(task, config, root, transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json=service_payload(task, status=status, anomaly_score=score, quality_passed=quality))))
    monkeypatch.setattr(detector, "inspect", inspect)
    monkeypatch.setattr(agent_module, "AnthropicModel", ScriptedModel)
    auth = app.auth(app.token("inspector.demo"))
    response = app.client.post("/api/v1/inspections", headers={**auth, "Idempotency-Key": "http-detector-test"},
        data={"batch_id": {"NORMAL": "BA-2026-1003", "ABNORMAL": "BA-2026-1005", "UNCERTAIN": "BA-2026-1012", "ERROR": "BA-2026-1001"}[status], "product_code": "VALVE-A"},
        files={"image": ("part.png", png_bytes(), "image/png")})
    assert response.status_code == 202, response.json()
    task_id = response.json()["task_id"]
    # A deployment edit after queueing cannot change the pinned request.
    monkeypatch.setenv("DETECTOR_URL", "http://wrong.test/")
    for _ in range(5):
        factory = session_factory_for(str(app.app.state.engine.url))
        try: run_once(factory, task_id=task_id)
        finally: factory.kw["bind"].dispose()
    detail = app.client.get(f"/api/v1/inspections/{task_id}", headers=auth).json()
    assert detail["status"] == expected
    assert detail["detection_status"] == status
    assert detail["detector_mode"] == "http"
    assert len(calls) == 1 and calls[0]["endpoint"] == "http://detector.test/inspect"
    with app.app.state.session_factory() as db:
        assert bool(db.scalar(select(ReportRow).where(ReportRow.task_id == task_id))) == (expected == "WAITING_APPROVAL")


def test_unconfigured_http_product_rejects_upload_without_mock_fallback(app, monkeypatch):
    monkeypatch.setenv("DETECTOR_PROFILES_PATH", "no-such-file.json")
    auth = app.auth(app.token("inspector.demo"))
    result = app.client.post("/api/v1/inspections", headers={**auth, "Idempotency-Key": "unconfigured-http"},
        data={"batch_id": "BA-2026-1001", "product_code": "VALVE-A"}, files={"image": ("part.png", png_bytes(), "image/png")})
    assert result.status_code == 503
    assert result.json()["error_code"] == "DETECTOR_NOT_CONFIGURED"
    with app.app.state.session_factory() as db:
        assert db.scalar(select(InspectionRow)) is None
