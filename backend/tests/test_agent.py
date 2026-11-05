import json

import pytest
from sqlalchemy import select

from test_api import TestAPI as Harness, PASSWORDS, png_bytes
from test_worker import queue
import qualityflow.agent_graph as agent_module
from qualityflow.checkpoints import DatabaseSaver
from qualityflow.db import (ApprovalRow, GraphCheckpointRow, InspectionRow, PipelineJobRow,
                            ReportRow, ToolExecutionRow, WorkOrderRow)
from qualityflow.worker import run_once, session_factory_for
from qualityflow.db import utc_now
import qualityflow.worker as worker_module


class ScriptedModel:
    def complete(self, messages):
        context = json.loads(messages[0]["content"])
        if len(messages) == 1:
            return [{"type": "tool_use", "id": str(index), "name": name, "input": args}
                    for index, (name, args) in enumerate([
                        ("get_batch_info", {}), ("get_inspection_history", {}),
                        ("search_sop_documents", {"query": "阀体表面反光"})])]
        return [{"type": "tool_use", "id": "final", "name": "submit_report", "input": {
            "defect_summary": "疑似表面异常，需实物复核", "observed_facts": [{"text": "检测标记异常",
                "evidence_ids": [context["evidence"][0]["id"]]}], "hypotheses": [],
            "missing_evidence": ["复拍图像"], "recommended_actions": ["现场复核"],
            "risk_level": "HIGH", "evidence_sufficient": False}}]


@pytest.fixture
def harness():
    value = Harness()
    value.setup_method()
    value.app.state.agent_mode = "llm"
    yield value
    value.teardown_method()


def test_agent_nodes_and_approval_resume_survive_new_worker_instances(harness, monkeypatch):
    monkeypatch.setattr(agent_module, "AnthropicModel", ScriptedModel)
    inspector = harness.auth(harness.token("inspector.demo"))
    result = harness.client.post("/api/v1/inspections", headers={**inspector, "Idempotency-Key": "agent-flow-1"},
        data={"batch_id": "BA-2026-1003", "product_code": "VALVE-A"},
        files={"image": ("sample.png", png_bytes(), "image/png")})
    assert result.status_code == 202
    task_id = result.json()["task_id"]
    factory = harness.app.state.session_factory
    for _ in range(4):
        restarted = session_factory_for(str(harness.app.state.engine.url))
        try:
            assert run_once(restarted, task_id=task_id)
        finally:
            restarted.kw["bind"].dispose()
    with factory() as db:
        job = db.scalar(select(PipelineJobRow).where(PipelineJobRow.task_id == task_id))
        assert job.status == "WAITING_APPROVAL"
        approval = db.scalar(select(ApprovalRow).where(ApprovalRow.task_id == task_id))
        approval_id = approval.id
        assert len(db.scalars(select(ToolExecutionRow)).all()) == 3
        assert len(db.scalars(select(GraphCheckpointRow)).all()) >= 4
        assert list(DatabaseSaver(db).list({"configurable": {"thread_id": f"demo-factory:{task_id}:v1"}}, limit=2))
    assert not run_once(factory, task_id=task_id)
    engineer = harness.auth(harness.token("engineer.demo"))
    manager = harness.auth(harness.token("manager.demo"))
    for auth, version in [(engineer, 0), (manager, 1)]:
        response = harness.client.post(f"/api/v1/approvals/{approval_id}/decision", headers=auth,
            json={"decision": "APPROVE", "reason": "核对证据", "expected_version": version, "expected_report_version": 1})
        assert response.status_code == 200, response.json()
    assert run_once(factory, task_id=task_id)
    with factory() as db:
        assert db.scalar(select(PipelineJobRow).where(PipelineJobRow.task_id == task_id)).status == "SUCCEEDED"
        assert len(db.scalars(select(ToolExecutionRow)).all()) == 3
        assert len(db.scalars(select(ReportRow)).all()) == 1
        assert len(db.scalars(select(WorkOrderRow)).all()) == 1
        checkpoint = DatabaseSaver(db).get_tuple({"configurable": {"thread_id": f"demo-factory:{task_id}:v1"}})
        assert checkpoint.checkpoint["channel_values"]["phase"] == "FINISHED"


@pytest.mark.parametrize("fault,expected", [
    ("write", "AGENT_TOOL_FORBIDDEN"), ("schema", "REPORT_SCHEMA_INVALID"),
    ("citation", "EVIDENCE_VALIDATION_FAILED"), ("risk", "REPORT_RISK_BELOW_POLICY"),
    ("budget", "AGENT_BUDGET_EXCEEDED"), ("early", "AGENT_TOOLS_INCOMPLETE")])
def test_agent_invalid_outputs_stop_before_approval(queue, monkeypatch, fault, expected):
    factory, _ = queue
    class BrokenModel(ScriptedModel):
        def complete(self, messages):
            result = super().complete(messages)
            if len(messages) == 1:
                if fault == "write":
                    result[0]["name"] = "create_work_order"
                if fault == "budget":
                    result = result * 3
                if fault == "early":
                    result = super().complete([messages[0], {"role": "user", "content": "x"}])
            elif fault == "schema":
                result[0]["input"]["unexpected"] = True
            elif fault == "citation":
                result[0]["input"]["observed_facts"][0]["evidence_ids"] = ["NOT-EXISTS"]
            elif fault == "risk":
                result[0]["input"]["risk_level"] = "LOW"
            return result
    monkeypatch.setattr(agent_module, "AnthropicModel", BrokenModel)
    with factory() as db:
        db.get(PipelineJobRow, "job").mode = "llm"
        db.commit()
    for _ in range(6):
        if not run_once(factory):
            break
    with factory() as db:
        task = db.get(InspectionRow, "task")
        assert task.status == "NEEDS_REVIEW" and task.error_code == expected
        assert not db.scalars(select(ApprovalRow)).all()
        assert not db.scalars(select(WorkOrderRow)).all()


def test_failed_node_rolls_back_checkpoint_and_tool_records_together(queue, monkeypatch):
    factory, _ = queue
    monkeypatch.setattr(agent_module, "AnthropicModel", ScriptedModel)
    with factory() as db:
        db.get(PipelineJobRow, "job").mode = "llm"
        db.commit()
    assert run_once(factory)  # Model selection saved; next node is the tools node.
    original = worker_module.execute_pipeline
    def crash_after_node(db, task_id):
        original(db, task_id)
        raise RuntimeError("synthetic crash before node commit")
    with factory() as db:
        count = len(db.scalars(select(GraphCheckpointRow)).all())
    monkeypatch.setattr(worker_module, "execute_pipeline", crash_after_node)
    run_once(factory)
    with factory() as db:
        assert len(db.scalars(select(GraphCheckpointRow)).all()) == count
        assert not db.scalars(select(ToolExecutionRow)).all()
        db.get(PipelineJobRow, "job").available_at = utc_now()
        db.commit()
    monkeypatch.setattr(worker_module, "execute_pipeline", original)
    assert run_once(factory)
    with factory() as db:
        assert len(db.scalars(select(ToolExecutionRow)).all()) == 3


def test_schema_repair_remains_bounded_and_can_produce_valid_report(queue, monkeypatch):
    factory, _ = queue
    class RepairableModel(ScriptedModel):
        def complete(self, messages):
            result = super().complete(messages)
            if len(messages) == 3:
                result[0]["input"]["observed_facts"] = ["incorrect fact shape"]
            return result
    monkeypatch.setattr(agent_module, "AnthropicModel", RepairableModel)
    with factory() as db:
        db.get(PipelineJobRow, "job").mode = "llm"
        db.commit()
    for _ in range(5):
        assert run_once(factory)
    with factory() as db:
        assert db.get(PipelineJobRow, "job").status == "WAITING_APPROVAL"
        assert len(db.scalars(select(ReportRow)).all()) == 1
        checkpoint = DatabaseSaver(db).get_tuple({"configurable": {"thread_id": "tenant:task:v1"}})
        assert checkpoint.checkpoint["channel_values"]["repairs"] == 1
