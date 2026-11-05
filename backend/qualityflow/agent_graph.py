"""Single-agent graph, advanced one durable node per worker transaction."""
import json
import os
from time import monotonic
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError
from sqlalchemy import select

from .checkpoints import DatabaseSaver
from .db import ApprovalRow, AuditRow, BatchRow, InspectionRow, PipelineJobRow, utc_now
from .investigation import investigation_risk, batch_snapshot_version, validate_evidence
from .model import AnthropicModel, ModelError, ReportDraft
from .pipeline import publish_report
from .tools import ReadOnlyTools, ToolName
from .supplements import evidence_prefix, supplement_for, supplement_evidence, return_context
from .detector import apply_detection


class State(TypedDict, total=False):
    messages: list
    sources: list
    trace: list
    calls: int
    rounds: int
    elapsed_ms: int
    selection: list
    draft: dict
    approval_id: str
    phase: str
    error: str
    repairs: int
    risk_floor: str


def build_graph(db, task, saver, model, report_version=1):
    def agent(state):
        if state["rounds"] >= 10 or state["elapsed_ms"] >= 120000:
            return {"error": "AGENT_BUDGET_EXCEEDED"}
        started = monotonic()
        try:
            selection = model.complete(state["messages"])
        except ModelError as exc:
            if exc.retryable:
                raise
            return {"error": exc.code}
        elapsed = state["elapsed_ms"] + int((monotonic() - started) * 1000)
        if elapsed >= 120000:
            return {"error": "AGENT_BUDGET_EXCEEDED"}
        if len(selection) > 8:
            return {"error": "AGENT_BUDGET_EXCEEDED"}
        if not selection or any(item.get("type") != "tool_use" for item in selection):
            return {"error": "AGENT_INVALID_TOOL_CALL"}
        finish = [item for item in selection if item.get("name") == "submit_report"]
        update = {"elapsed_ms": elapsed, "rounds": state["rounds"] + 1}
        if finish:
            if len(selection) != 1 or len({item["tool_name"] for item in state["trace"]}) < 3:
                return {"error": "AGENT_TOOLS_INCOMPLETE"}
            try:
                draft = ReportDraft.model_validate(finish[0]["input"]).model_dump()
            except (KeyError, ValidationError) as exc:
                fields = [{"field": ".".join(map(str, item["loc"])), "type": item["type"]}
                          for item in exc.errors()] if isinstance(exc, ValidationError) else []
                if state.get("repairs", 0) >= 2:
                    return {"error": "REPORT_SCHEMA_INVALID"}
                return {**update, "phase": "REPAIR", "repairs": state.get("repairs", 0) + 1,
                        "messages": [*state["messages"], {"role": "assistant", "content": finish},
                            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": finish[0]["id"],
                             "content": json.dumps({"error": "REPORT_SCHEMA_INVALID", "fields": fields,
                                "instruction": "按 submit_report Schema 修正后重新提交；只列出规定字段。"}, ensure_ascii=False)}]}]}
            levels = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
            if levels[draft["risk_level"]] < levels[state["risk_floor"]]:
                return {"error": "REPORT_RISK_BELOW_POLICY"}
            if draft["evidence_sufficient"]:
                return {"error": "REPORT_SINGLE_IMAGE_INSUFFICIENT"}
            return {**update, "draft": draft, "phase": "PUBLISH"}
        if any(item.get("name") not in {name.value for name in ToolName} for item in selection):
            return {"error": "AGENT_TOOL_FORBIDDEN"}
        if state["calls"] + len(selection) > 8:
            return {"error": "AGENT_BUDGET_EXCEEDED"}
        return {**update, "selection": selection, "phase": "TOOLS",
                "messages": [*state["messages"], {"role": "assistant", "content": selection}]}

    def tools(state):
        started = monotonic()
        with saver.lock:
            service = ReadOnlyTools(db, tenant_id=task.tenant_id, task_id=task.id, report_version=report_version)
            service.calls = state["calls"]
            sources = {item["id"]: item for item in state["sources"]}
            results = []
            for call in state["selection"]:
                result = service.run(ToolName(call["name"]), call.get("input", {}))
                sources.update({item["id"]: item for item in result["evidence"]})
                results.append({"type": "tool_result", "tool_use_id": call["id"],
                                "content": json.dumps(result, ensure_ascii=False)})
            return {"sources": list(sources.values()), "calls": service.calls,
                    "trace": [*state["trace"], *service.trace],
                    "elapsed_ms": state["elapsed_ms"] + int((monotonic() - started) * 1000),
                    "messages": [*state["messages"], {"role": "user", "content": results}]}

    def publish(state):
        with saver.lock:
            batch = db.get(BatchRow, task.batch_id)
            draft = {**state["draft"], "model_version": os.environ.get("LLM_MODEL", "unknown"),
                     "prompt_version": "quality-agent-v1", "generated_at": utc_now().isoformat(),
                     "evidence_refs": state["sources"], "tool_trace": state["trace"],
                     "retrieval_method": "char-ngram-tfidf-cosine-v1", "analysis_mode": "llm"}
            draft["missing_evidence"] = [*draft["missing_evidence"], *[
                f"{item['tool_name']}：{item['error_code'] or '无结果'}"
                for item in state["trace"] if item["status"] in {"ERROR", "EMPTY"}]]
            supplement = supplement_for(db, task.id, report_version)
            draft["supplement_context"] = return_context(db, supplement) if supplement else None
            if validate_evidence(draft, state["sources"], task, batch, db, report_version):
                return {"error": "EVIDENCE_VALIDATION_FAILED"}
            _, approval_id = publish_report(db, task, draft, state["sources"], report_version)
            task.version += 1
            return {"approval_id": approval_id, "phase": "APPROVAL"}

    def approval(state):
        interrupt({"approval_id": state["approval_id"], "task_id": task.id})
        with saver.lock:
            row = db.get(ApprovalRow, state["approval_id"])
            if row is None or row.status == "PENDING":
                return {"error": "APPROVAL_NOT_FINAL"}
            return {"phase": "FINISHED"}

    graph = StateGraph(State)
    for name, node in [("agent", agent), ("tools", tools), ("publish", publish), ("approval", approval)]:
        graph.add_node(name, node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", lambda s: END if s.get("error") else "publish" if s.get("phase") == "PUBLISH" else "agent" if s.get("phase") == "REPAIR" else "tools")
    graph.add_edge("tools", "agent")
    graph.add_conditional_edges("publish", lambda s: END if s.get("error") else "approval")
    graph.add_edge("approval", END)
    return graph.compile(checkpointer=saver, interrupt_after=["agent", "tools"])


def run_agent_step(db, task_id, model=None):
    task = db.scalar(select(InspectionRow).where(InspectionRow.id == task_id).with_for_update())
    if task is None:
        return "SUCCEEDED"
    job = db.scalar(select(PipelineJobRow).where(PipelineJobRow.task_id == task.id))
    version = job.report_version if job else 1
    saver = DatabaseSaver(db)
    graph = build_graph(db, task, saver, model or AnthropicModel(), version)
    config = {"configurable": {"thread_id": f"{task.tenant_id}:{task.id}:v{version}"}}
    snapshot = graph.get_state(config)
    initial = None
    if not snapshot.values:
        if task.status != "PENDING":
            return "SUCCEEDED"
        batch = db.get(BatchRow, task.batch_id)
        if version == 1:
            apply_detection(db, task, batch)
            if task.status != "INVESTIGATING":
                task.version += 1
                return "SUCCEEDED"
        task.status = "INVESTIGATING"
        task.version += 1
        now = utc_now().isoformat()
        prefix = evidence_prefix(task.id, version)
        sources = [{"id": prefix + "-D", "source_type": "detection", "source_id": task.id,
                    "source_version": task.model_version, "title": "视觉检测结果",
                    "excerpt": f"{task.detection_status}, score={task.anomaly_score}", "validity": "CURRENT", "retrieved_at": now},
                   {"id": prefix + "-B", "source_type": "batch", "source_id": batch.id,
                    "source_version": batch_snapshot_version(batch), "title": "生产批次",
                    "excerpt": f"{batch.id}, {batch.product_code}", "validity": "CURRENT", "retrieved_at": now}]
        floor = investigation_risk(task, batch)
        supplement = supplement_for(db, task.id, version)
        context = return_context(db, supplement) if supplement else None
        if supplement:
            sources.append(supplement_evidence(supplement))
            levels = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
            if levels.get(context["previous_risk_level"], 1) > levels.get(floor, 1):
                floor = context["previous_risk_level"]
        initial = {"messages": [{"role": "user", "content": json.dumps({"task_id": task.id,
            "product_code": task.product_code, "detection": task.detection_status,
            "risk_floor": floor, "report_version": version, "supplement_context_untrusted": context,
            "remark_untrusted": task.remark, "evidence": sources}, ensure_ascii=False)}],
            "sources": sources, "calls": 0, "rounds": 0, "elapsed_ms": 0, "trace": [], "risk_floor": floor}
    elif any(item.interrupts for item in snapshot.tasks):
        row = db.get(ApprovalRow, snapshot.values["approval_id"])
        if row is None or row.status == "PENDING":
            return "WAITING_APPROVAL"
        initial = Command(resume={"source": "business_database"})
    result = graph.invoke(initial, config, durability="sync")
    if result.get("error"):
        task.status, task.error_code = "NEEDS_REVIEW", result["error"]
        task.version += 1
        db.add(AuditRow(id=str(uuid4()), tenant_id=task.tenant_id, actor_id=None,
            action="AGENT_FAILED", resource_type="inspection", resource_id=task.id,
            payload={"error_code": task.error_code}))
        return "SUCCEEDED"
    snapshot = graph.get_state(config)
    if any(item.interrupts for item in snapshot.tasks):
        row = db.get(ApprovalRow, snapshot.values["approval_id"])
        return "WAITING_APPROVAL" if row.status == "PENDING" else "READY"
    return "READY" if snapshot.next else "SUCCEEDED"


def finish_returned_checkpoint(db, task_id):
    class NoModelCalls:
        def complete(self, _messages):
            raise RuntimeError("RETURNED_CHECKPOINT_NOT_PAUSED")
    if run_agent_step(db, task_id, model=NoModelCalls()) != "SUCCEEDED":
        raise RuntimeError("RETURNED_CHECKPOINT_NOT_FINAL")
