from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from qualityflow.api import create_app
from qualityflow.db import (AuditRow, BatchRow, InspectionRow, KnowledgeRow,
                            ToolExecutionRow, UserRow, WorkOrderRow, utc_now)
from qualityflow.investigation import mock_investigation, validate_evidence
from qualityflow.tools import ReadOnlyTools, ToolName


@pytest.fixture
def context(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'tools.db'}", create_schema=True)
    with app.state.session_factory() as db:
        db.add(UserRow(id="inspector", tenant_id="tenant-a", username="inspector", display_name="测试",
                       role="INSPECTOR", password_hash="not-used"))
        db.add(BatchRow(id="batch", tenant_id="tenant-a", product_code="VALVE-A",
                        equipment_id="equipment", start_at=utc_now(), end_at=utc_now(), scenario_id="SC-03"))
        db.flush()
        task = InspectionRow(id="task", tenant_id="tenant-a", created_by="inspector", batch_id="batch",
            product_code="VALVE-A", image_key="test", image_sha256="a" * 64, image_mime="image/png",
            image_width=8, image_height=8, status="INVESTIGATING", detection_status="ABNORMAL",
            anomaly_score=0.87, model_version="mock-detector-v1", idempotency_key="test-key", request_hash="b" * 64)
        db.add(task)
        data = json.loads((Path(__file__).resolve().parents[2] / "data/knowledge.json").read_text(encoding="utf-8"))
        for item in data:
            db.add(KnowledgeRow(id=item["key"], tenant_id="tenant-a", source_key=item["key"],
                source_type=item["type"], product_code=item["product"], version=item["version"],
                title=item["title"], content=item["content"], content_hash=sha256(item["content"].encode()).hexdigest(),
                valid_from=utc_now() - timedelta(days=10),
                valid_to=utc_now() - timedelta(days=1) if item["valid_to"] else None, active=True))
        db.commit()
        yield db, task
    app.state.engine.dispose()


def test_retrieval_recalls_expected_sources_and_does_not_write_business_data(context):
    db, task = context
    tools = ReadOnlyTools(db, tenant_id=task.tenant_id, task_id=task.id)
    assert tools.run(ToolName.BATCH)["data"][0]["batch_id"] == "batch"
    assert tools.run(ToolName.HISTORY)["status"] == "EMPTY"
    sop = tools.run(ToolName.SOP, {"query": "阀体表面异常 反光 补拍"})
    case = tools.run(ToolName.CASES, {"query": "阀体表面异常 反光 补拍"})
    assert [item["source_id"] for item in sop["data"]] == ["SOP-SURFACE"]
    assert [item["source_id"] for item in case["data"]] == ["CASE-REFLECTION"]
    db.flush()
    assert len(db.scalars(select(ToolExecutionRow)).all()) == 4
    assert not db.scalars(select(WorkOrderRow)).all()
    assert task.status == "INVESTIGATING"


def test_retrieval_filters_tenant_product_validity_and_preserves_empty_results(context):
    db, task = context
    for key, tenant, start, active in [
        ("foreign", "tenant-b", -1, True), ("future", "tenant-a", 10, True),
        ("disabled", "tenant-a", -1, False)]:
        text = "阀体表面异常 反光 补拍"
        db.add(KnowledgeRow(id=key, tenant_id=tenant, source_key=key, source_type="sop",
            product_code="VALVE-A", version="v1", title=text, content=text,
            content_hash=sha256(text.encode()).hexdigest(), valid_from=utc_now() + timedelta(days=start),
            valid_to=None, active=active))
    db.flush()
    tools = ReadOnlyTools(db, tenant_id=task.tenant_id, task_id=task.id)
    result = tools.run(ToolName.SOP, {"query": "阀体表面异常 反光 补拍"})
    assert [item["source_id"] for item in result["data"]] == ["SOP-SURFACE"]
    empty = tools.run(ToolName.CASES, {"query": "镀层起泡未收录"})
    assert empty["status"] == "EMPTY" and empty["data"] == [] and empty["evidence"] == []


def test_tool_errors_budget_and_cross_tenant_context_are_explicit(context):
    db, task = context
    tools = ReadOnlyTools(db, tenant_id=task.tenant_id, task_id=task.id, max_calls=2)
    assert tools.run(ToolName.BATCH, {"batch_id": "foreign"})["error_code"] == "FORBIDDEN"
    assert tools.run(ToolName.HISTORY, {"limit": 100})["error_code"] == "INVALID_TOOL_ARGUMENTS"
    assert tools.run(ToolName.SOP, {"query": "阀体"})["error_code"] == "TOOL_BUDGET_EXCEEDED"
    foreign = ReadOnlyTools(db, tenant_id="tenant-b", task_id=task.id)
    assert foreign.run(ToolName.BATCH)["error_code"] == "FORBIDDEN"
    db.flush()
    assert len(db.scalars(select(ToolExecutionRow)).all()) == 3
    assert len(db.scalars(select(AuditRow)).all()) == 4


def test_report_validator_rejects_changed_or_foreign_document_sources(context):
    db, task = context
    batch = db.get(BatchRow, task.batch_id)
    report, evidence = mock_investigation(task, batch, db)
    assert not validate_evidence(report, evidence, task, batch, db)
    source = db.get(KnowledgeRow, "SOP-SURFACE")
    original = source.content
    source.content += "已篡改"
    assert any("SOURCE_MISMATCH" in item for item in validate_evidence(report, evidence, task, batch, db))
    source.content = original
    source.tenant_id = "tenant-b"
    assert any("SOURCE_MISMATCH" in item for item in validate_evidence(report, evidence, task, batch, db))


def test_history_has_its_own_versioned_evidence_and_rejects_tampering(context):
    db, task = context
    previous = InspectionRow(id="previous", tenant_id=task.tenant_id, created_by=task.created_by,
        batch_id=task.batch_id, product_code=task.product_code, image_key="old", image_sha256="c" * 64,
        image_mime="image/png", image_width=8, image_height=8, status="COMPLETED",
        detection_status="NORMAL", model_version="history-v1", idempotency_key="old-key",
        request_hash="d" * 64, created_at=task.created_at - timedelta(days=1))
    db.add(previous)
    db.flush()
    result = ReadOnlyTools(db, tenant_id=task.tenant_id, task_id=task.id).run(ToolName.HISTORY)
    assert len(result["evidence"]) == 1
    evidence = result["evidence"]
    assert evidence[0]["source_type"] == "inspection_history" and evidence[0]["validity"] == "HISTORICAL"
    report = {"observed_facts": [{"text": "既往检测返回正常", "evidence_ids": [evidence[0]["id"]]}], "hypotheses": []}
    batch = db.get(BatchRow, task.batch_id)
    assert not validate_evidence(report, evidence, task, batch, db)
    previous.model_version = "changed-model"
    assert validate_evidence(report, evidence, task, batch, db)
    previous.model_version = "history-v1"
    previous.tenant_id = "other-tenant"
    assert validate_evidence(report, evidence, task, batch, db)
