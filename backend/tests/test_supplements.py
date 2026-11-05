from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
from threading import Barrier
import json

import pytest
from sqlalchemy import select

from test_api import TestAPI as Harness, png_bytes
from test_agent import ScriptedModel
import qualityflow.agent_graph as agent_module
from qualityflow.checkpoints import DatabaseSaver
from qualityflow.db import (ApprovalRow, EvidenceRow, GraphCheckpointRow, InspectionRow, PipelineJobRow,
                            ReportRow, SessionRow, SupplementRow, ToolExecutionRow, UserRow, WorkOrderRow, utc_now)
from qualityflow.worker import run_once, session_factory_for


@pytest.fixture
def app():
    value = Harness()
    value.setup_method()
    yield value
    value.teardown_method()


class RoundModel(ScriptedModel):
    def complete(self, messages):
        result = super().complete(messages)
        if len(messages) > 1:
            context = json.loads(messages[0]["content"])
            for source in context["evidence"]:
                if source["source_type"] == "supplement":
                    result[0]["input"]["observed_facts"].append({
                        "text": "质检员补证陈述待核验", "evidence_ids": [source["id"]]})
        return result


def drain(app, task_id, restart=False):
    for _ in range(15):
        factory = session_factory_for(str(app.app.state.engine.url)) if restart else app.app.state.session_factory
        try:
            if not run_once(factory, task_id=task_id):
                break
        finally:
            if restart:
                factory.kw["bind"].dispose()


def create_returned(app, monkeypatch=None):
    if monkeypatch:
        app.app.state.agent_mode = "llm"
        monkeypatch.setattr(agent_module, "AnthropicModel", RoundModel)
    inspector = app.auth(app.token("inspector.demo"))
    engineer = app.auth(app.token("engineer.demo"))
    manager = app.auth(app.token("manager.demo"))
    response = app.client.post("/api/v1/inspections", headers={**inspector, "Idempotency-Key": "round-original"},
        data={"batch_id": "BA-2026-1003", "product_code": "VALVE-A"},
        files={"image": ("original.png", png_bytes(), "image/png")})
    assert response.status_code == 202, response.json()
    task_id = response.json()["task_id"]
    drain(app, task_id)
    detail = app.client.get(f"/api/v1/inspections/{task_id}", headers=inspector).json()
    approval_id = detail["approval_id"]
    version = 0
    reviewer = engineer
    if monkeypatch:
        approved = app.client.post(f"/api/v1/approvals/{approval_id}/decision", headers=engineer,
            json={"decision": "APPROVE", "reason": "工程师核对", "expected_version": 0, "expected_report_version": 1})
        assert approved.status_code == 200
        version, reviewer = 1, manager
    returned = app.client.post(f"/api/v1/approvals/{approval_id}/decision", headers=reviewer,
        json={"decision": "RETURN_FOR_MORE_EVIDENCE", "reason": "补拍同位置并说明光照条件",
              "expected_version": version, "expected_report_version": 1})
    assert returned.status_code == 200, returned.json()
    return task_id, inspector, engineer, manager


def submit(app, task_id, inspector, key="round-supplement", note="重新取样，调整光照后仍有疑似划痕", **overrides):
    detail = app.client.get(f"/api/v1/inspections/{task_id}", headers=inspector).json()
    data = {"expected_version": detail["version"], "expected_report_version": detail["latest_report_version"], "note": note}
    data.update(overrides)
    response = app.client.post(f"/api/v1/inspections/{task_id}/supplements",
        headers={**inspector, "Idempotency-Key": key}, data=data,
        files={"image": ("supplement.png", png_bytes("black"), "image/png")})
    return response, data


def approve_new(app, task_id, engineer, manager, version):
    detail = app.client.get(f"/api/v1/inspections/{task_id}", headers=engineer).json()
    approval_id = detail["approval_id"]
    result = app.client.post(f"/api/v1/approvals/{approval_id}/decision", headers=engineer,
        json={"decision": "APPROVE", "reason": "核对补证", "expected_version": 0, "expected_report_version": version})
    assert result.status_code == 200, result.json()
    assert result.json()["work_order_id"] is None and result.json()["stage"] == "MANAGER"
    for _ in range(2):
        result = app.client.post(f"/api/v1/approvals/{approval_id}/decision", headers=manager,
            json={"decision": "APPROVE", "reason": "独立核对补证", "expected_version": 1, "expected_report_version": version})
        assert result.status_code == 200, result.json()
    return result.json()["work_order_id"]


def test_supplement_v2_preserves_original_history_and_idempotency(app):
    task_id, inspector, engineer, manager = create_returned(app)
    base = f"/api/v1/inspections/{task_id}"
    original_report = app.client.get(base + "/report?version=1", headers=inspector).json()
    old_id = original_report["approval_id"]
    with app.app.state.session_factory() as db:
        task = db.get(InspectionRow, task_id)
        original_image = (task.image_key, task.image_sha256)
    first, body = submit(app, task_id, inspector)
    assert first.status_code == 202, first.json()
    supplement_id = first.json()["supplement_id"]
    stale = app.client.post(f"/api/v1/approvals/{old_id}/decision", headers=engineer,
        json={"decision": "APPROVE", "reason": "旧报告", "expected_version": 1, "expected_report_version": 1})
    assert stale.status_code == 409
    replay = app.client.post(base + "/supplements", headers={**inspector, "Idempotency-Key": "round-supplement"},
        data=body, files={"image": ("retry.png", png_bytes("black"), "image/png")})
    assert replay.status_code == 202 and replay.json()["replayed"] is True
    assert replay.json()["supplement_id"] == supplement_id
    mismatch, _ = submit(app, task_id, inspector, note="不同内容", expected_report_version=1)
    assert mismatch.status_code == 409 and mismatch.json()["error_code"] == "IDEMPOTENCY_KEY_REUSED"
    drain(app, task_id, restart=True)
    current = app.client.get(base + "/report", headers=inspector).json()
    assert current["report_version"] == 2 and current["report_id"] != original_report["report_id"]
    assert app.client.get(base + "/report?version=1", headers=inspector).json() == original_report
    assert {item["id"] for item in current["evidence_refs"]}.isdisjoint({item["id"] for item in original_report["evidence_refs"]})
    assert any(item["source_type"] == "supplement" for item in current["evidence_refs"])
    assert [item["report_version"] for item in app.client.get(base + "/reports", headers=inspector).json()["items"]] == [2, 1]
    image = app.client.get(base + f"/supplements/{supplement_id}/image", headers=inspector)
    assert image.status_code == 200 and image.content == png_bytes("black")
    original = app.client.get(base + "/image", headers=inspector)
    assert original.status_code == 200 and original.content == png_bytes()
    order_id = approve_new(app, task_id, engineer, manager, 2)
    assert order_id
    with app.app.state.session_factory() as db:
        task = db.get(InspectionRow, task_id)
        assert (task.image_key, task.image_sha256) == original_image
        assert len(db.scalars(select(SupplementRow)).all()) == 1
        assert len(db.scalars(select(WorkOrderRow)).all()) == 1


def test_llm_round_v2_uses_new_checkpoint_and_resumes_after_restarts(app, monkeypatch):
    task_id, inspector, engineer, manager = create_returned(app, monkeypatch)
    first, _ = submit(app, task_id, inspector)
    assert first.status_code == 202, first.json()
    factory = app.app.state.session_factory
    with factory() as db:
        old = DatabaseSaver(db).get_tuple({"configurable": {"thread_id": f"demo-factory:{task_id}:v1"}})
        assert old.checkpoint["channel_values"]["phase"] == "FINISHED"
    drain(app, task_id, restart=True)
    report = app.client.get(f"/api/v1/inspections/{task_id}/report", headers=engineer).json()
    assert report["report_version"] == 2 and report["analysis_mode"] == "llm"
    assert report["supplement_context"]["return_reason"] == "补拍同位置并说明光照条件"
    approve_new(app, task_id, engineer, manager, 2)
    drain(app, task_id, restart=True)
    with factory() as db:
        assert db.scalar(select(PipelineJobRow).where(PipelineJobRow.task_id == task_id)).status == "SUCCEEDED"
        latest = DatabaseSaver(db).get_tuple({"configurable": {"thread_id": f"demo-factory:{task_id}:v2"}})
        assert latest.checkpoint["channel_values"]["phase"] == "FINISHED"
        assert len(db.scalars(select(ReportRow)).all()) == 2
        assert len(db.scalars(select(ToolExecutionRow)).all()) == 6


def test_supplement_guards_role_owner_tenant_content_and_version(app):
    task_id, inspector, engineer, _ = create_returned(app)
    response, _ = submit(app, task_id, engineer)
    assert response.status_code == 403
    response, _ = submit(app, task_id, inspector, expected_version=-1)
    assert response.status_code == 409
    response, _ = submit(app, task_id, inspector, note=" ")
    assert response.status_code == 422
    base = f"/api/v1/inspections/{task_id}/supplements"
    detail = app.client.get(f"/api/v1/inspections/{task_id}", headers=inspector).json()
    bad = app.client.post(base, headers={**inspector, "Idempotency-Key": "bad-image-key"},
        data={"expected_version": detail["version"], "expected_report_version": 1, "note": "说明"},
        files={"image": ("fake.png", b"not an image", "image/png")})
    assert bad.status_code == 422
    for index, tenant in enumerate(["demo-factory", "other-tenant"]):
        token = f"test-scope-token-{index}"
        with app.app.state.session_factory() as db:
            db.add(UserRow(id=token, tenant_id=tenant, username=token, display_name="其他质检员",
                           role="INSPECTOR", password_hash="unused", active=True, must_change_password=False))
            db.flush()
            db.add(SessionRow(token_hash=sha256(token.encode()).hexdigest(), user_id=token,
                              expires_at=utc_now() + timedelta(hours=1)))
            db.commit()
        denied = app.client.get(base, headers=app.auth(token))
        assert denied.status_code == 404
        denied = app.client.post(base, headers={**app.auth(token), "Idempotency-Key": "scope-test"},
            data={"expected_version": detail["version"], "expected_report_version": 1, "note": "越权"},
            files={"image": ("sample.png", png_bytes(), "image/png")})
        assert denied.status_code == 404
    with app.app.state.session_factory() as db:
        assert not db.scalars(select(SupplementRow)).all()


def test_two_concurrent_supplements_start_only_one_round(app):
    task_id, inspector, _, _ = create_returned(app)
    detail = app.client.get(f"/api/v1/inspections/{task_id}", headers=inspector).json()
    barrier = Barrier(2)
    def post(index):
        barrier.wait()
        return app.client.post(f"/api/v1/inspections/{task_id}/supplements",
            headers={**inspector, "Idempotency-Key": f"concurrent-round-{index}"},
            data={"expected_version": detail["version"], "expected_report_version": 1, "note": "并发补证"},
            files={"image": ("supplement.png", png_bytes("black"), "image/png")}).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(post, range(2)))
    assert sorted(statuses) == [202, 409]
    with app.app.state.session_factory() as db:
        assert len(db.scalars(select(SupplementRow)).all()) == 1
        assert db.scalar(select(PipelineJobRow)).report_version == 2


def test_multiple_returns_make_v3_and_old_replay_does_not_reset_new_round(app):
    task_id, inspector, engineer, _ = create_returned(app)
    response, old_body = submit(app, task_id, inspector)
    assert response.status_code == 202
    drain(app, task_id)
    detail = app.client.get(f"/api/v1/inspections/{task_id}", headers=engineer).json()
    returned = app.client.post(f"/api/v1/approvals/{detail['approval_id']}/decision", headers=engineer,
        json={"decision": "RETURN_FOR_MORE_EVIDENCE", "reason": "再补充另一角度",
              "expected_version": 0, "expected_report_version": 2})
    assert returned.status_code == 200
    third, _ = submit(app, task_id, inspector, key="round-v3", note="补充另一角度的观察说明")
    assert third.status_code == 202 and third.json()["report_version"] == 3
    replay = app.client.post(f"/api/v1/inspections/{task_id}/supplements",
        headers={**inspector, "Idempotency-Key": "round-supplement"}, data=old_body,
        files={"image": ("same.png", png_bytes("black"), "image/png")})
    assert replay.status_code == 202 and replay.json()["report_version"] == 2
    drain(app, task_id)
    with app.app.state.session_factory() as db:
        assert db.scalar(select(PipelineJobRow)).report_version == 3
        assert [r.version for r in db.scalars(select(ReportRow).order_by(ReportRow.version)).all()] == [1, 2, 3]


def test_tampered_supplement_cannot_publish_new_approval(app):
    task_id, inspector, _, _ = create_returned(app)
    result, _ = submit(app, task_id, inspector)
    assert result.status_code == 202
    with app.app.state.session_factory() as db:
        db.get(SupplementRow, result.json()["supplement_id"]).note = "篡改说明"
        db.commit()
    drain(app, task_id)
    with app.app.state.session_factory() as db:
        assert db.get(InspectionRow, task_id).error_code == "EVIDENCE_VALIDATION_FAILED"
        assert len(db.scalars(select(ReportRow)).all()) == 1
        assert len(db.scalars(select(ApprovalRow)).all()) == 1


def test_reports_not_returned_do_not_accept_supplements(app):
    inspector = app.auth(app.token("inspector.demo"))
    engineer = app.auth(app.token("engineer.demo"))
    manager = app.auth(app.token("manager.demo"))
    upload = app.client.post("/api/v1/inspections", headers={**inspector, "Idempotency-Key": "not-returned-task"},
        data={"batch_id": "BA-2026-1003", "product_code": "VALVE-A"},
        files={"image": ("original.png", png_bytes(), "image/png")})
    task_id = upload.json()["task_id"]
    drain(app, task_id)
    assert submit(app, task_id, inspector)[0].status_code == 409
    approve_new(app, task_id, engineer, manager, 1)
    assert submit(app, task_id, inspector)[0].status_code == 409
    with app.app.state.session_factory() as db:
        assert not db.scalars(select(SupplementRow)).all()
