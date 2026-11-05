"""Deterministic investigation template for the synthetic MVP scenarios.

It cites detector, batch and actually retrieved reference records.
The template never claims a confirmed root cause or invents case/SOP citations.
"""

from __future__ import annotations

from hashlib import sha256
from datetime import timezone

from sqlalchemy.orm import Session
from .db import BatchRow, InspectionRow, KnowledgeRow, SupplementRow, utc_now
from .tools import ReadOnlyTools, ToolName, current_document, document_version, history_excerpt, history_version
from .supplements import evidence_prefix, supplement_for, supplement_hash, supplement_evidence, return_context


SCENARIO_RISK = {
    "SC-01": "LOW", "SC-02": "MEDIUM", "SC-03": "HIGH",
    "SC-04": "MEDIUM", "SC-05": "UNKNOWN", "SC-06": "MEDIUM",
    "SC-07": "HIGH", "SC-08": "MEDIUM", "SC-09": "MEDIUM",
    "SC-10": "MEDIUM", "SC-11": "HIGH", "SC-12": "MEDIUM",
}


def investigation_risk(task, batch):
    # Synthetic scene labels cannot define the risk of an external visual result.
    if (task.detector_config or {}).get("mode") == "http":
        return "MEDIUM"
    return SCENARIO_RISK.get(batch.scenario_id or "", "MEDIUM")


def batch_snapshot_version(batch: BatchRow) -> str:
    data = "|".join([batch.id, batch.product_code, batch.equipment_id,
                     batch.start_at.isoformat(), batch.end_at.isoformat()])
    return sha256(data.encode("utf-8")).hexdigest()[:16]


def mock_investigation(task: InspectionRow, batch: BatchRow, db: Session, report_version: int = 1) -> tuple[dict, list[dict]]:
    """Return a report draft and source metadata; neither is persisted yet."""
    prefix = evidence_prefix(task.id, report_version)
    detection_id = prefix + "-D"
    batch_id = prefix + "-B"
    retrieved_at = utc_now().isoformat()
    evidence = [
        {"id": detection_id, "source_type": "detection", "source_id": task.id,
         "source_version": task.model_version or "", "retrieved_at": retrieved_at,
         "title": "视觉检测结果", "excerpt": f"状态 {task.detection_status}；异常分数 {task.anomaly_score}",
         "validity": "CURRENT"},
        {"id": batch_id, "source_type": "batch", "source_id": batch.id,
         "source_version": batch_snapshot_version(batch), "retrieved_at": retrieved_at,
         "title": "生产批次记录", "excerpt": f"产品 {batch.product_code}；设备 {batch.equipment_id}",
         "validity": "CURRENT"},
    ]
    tools = ReadOnlyTools(db, tenant_id=task.tenant_id, task_id=task.id, report_version=report_version)
    batch_result = tools.run(ToolName.BATCH)
    history_result = tools.run(ToolName.HISTORY, {"limit": 5})
    query = "镀层起泡未收录" if batch.scenario_id == "SC-06" else "阀体表面异常 划痕 反光 补拍"
    cases = tools.run(ToolName.CASES, {"query": query, "limit": 3})
    sop = tools.run(ToolName.SOP, {"query": query, "limit": 3})
    evidence.extend(cases["evidence"] + sop["evidence"])
    uncertain = task.detection_status == "UNCERTAIN"
    risk = investigation_risk(task, batch)
    facts = [
        {"text": "视觉检测返回不确定结果" if uncertain else "视觉检测标记疑似异常",
         "evidence_ids": [detection_id]},
        {"text": f"任务对应批次 {batch.id}，产品 {batch.product_code}",
         "evidence_ids": [batch_id]},
    ]
    for source in cases["evidence"] + sop["evidence"]:
        facts.append({"text": f"检索到参考资料：{source['title']}（适用性仍需现场核对）",
                      "evidence_ids": [source["id"]]})
    supplement = supplement_for(db, task.id, report_version)
    if supplement:
        item = supplement_evidence(supplement)
        evidence.append(item)
        facts.append({"text": "质检员追加补证说明（尚未独立核实）：" + supplement.note,
                      "evidence_ids": [item["id"]]})
        levels = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        previous_risk = return_context(db, supplement)["previous_risk_level"]
        if levels.get(previous_risk, 1) > levels.get(risk, 1):
            risk = previous_risk
    if batch.scenario_id == "SC-12" and (task.detector_config or {}).get("mode", "mock") == "mock":
        facts[0]["evidence_ids"] = ["EV-NOT-FOUND"]  # synthetic validation failure
    missing = ["同批次复拍图像", "现场设备事件记录", "检索资料与现场实际适用性核对"]
    if not cases["data"]:
        missing.append("历史质量案例检索无结果")
    if not sop["data"]:
        missing.append("适用 SOP 检索无结果")
    for result in (batch_result, history_result, cases, sop):
        if result["status"] == "ERROR":
            missing.append("只读工具失败：" + result["error_code"])
    report = {
        "defect_summary": "视觉结果不确定，需人工复核" if uncertain else "图像存在疑似表面异常，根因尚未确认",
        "observed_facts": facts,
        "hypotheses": [{"cause": "图像表面特征需现场区分真实缺陷与成像干扰",
                        "supporting_evidence_ids": [detection_id],
                        "contradicting_evidence_ids": [],
                        "uncertainty": "单张图像、批次元数据与检索参考资料仍不足以确认根因",
                        "verification_plan": "补拍同位置图像，抽检实物并核对设备事件"}],
        "missing_evidence": missing,
        "recommended_actions": ["质量工程师复核原图与检测结果", "补拍并抽检同批次样品"],
        "risk_level": risk,
        "evidence_sufficient": False,
        "model_version": task.model_version,
        "prompt_version": "mock-template-v2",
        "generated_at": retrieved_at,
        "evidence_refs": evidence,
        "tool_trace": tools.trace,
        "retrieval_method": "char-ngram-tfidf-cosine-v1",
        "supplement_context": return_context(db, supplement) if supplement else None,
    }
    return report, evidence


def validate_evidence(report: dict, evidence: list[dict], task: InspectionRow,
                      batch: BatchRow, db: Session | None = None, report_version: int = 1) -> list[str]:
    """Reject missing, stale, cross-task, or mismatched source references."""
    valid = {item["id"]: item for item in evidence}
    errors: list[str] = []
    if task.tenant_id != batch.tenant_id or task.batch_id != batch.id:
        errors.append("CROSS_TENANT_OR_BATCH_MISMATCH")
    cited = set()
    for fact in report["observed_facts"]:
        cited.update(fact["evidence_ids"])
    for hypothesis in report["hypotheses"]:
        cited.update(hypothesis["supporting_evidence_ids"])
        cited.update(hypothesis["contradicting_evidence_ids"])
    for evidence_id in sorted(cited):
        item = valid.get(evidence_id)
        if item is None:
            errors.append(f"MISSING:{evidence_id}")
            continue
        if item["validity"] != ("HISTORICAL" if item["source_type"] == "inspection_history" else "CURRENT"):
            errors.append(f"INVALID:{evidence_id}")
        if item["source_type"] == "detection":
            if item["source_id"] != task.id or item["source_version"] != task.model_version:
                errors.append(f"SOURCE_MISMATCH:{evidence_id}")
        elif item["source_type"] == "batch":
            if item["source_id"] != batch.id or item["source_version"] != batch_snapshot_version(batch):
                errors.append(f"SOURCE_MISMATCH:{evidence_id}")
        elif item["source_type"] == "inspection_history":
            source = db.get(InspectionRow, item["source_id"]) if db else None
            def aware(value):
                return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            if (source is None or source.id == task.id or source.tenant_id != task.tenant_id
                    or source.product_code != task.product_code or aware(source.created_at) > aware(task.created_at)
                    or history_version(source) != item["source_version"]
                    or history_excerpt(source) != item["excerpt"]):
                errors.append(f"SOURCE_MISMATCH:{evidence_id}")
        elif item["source_type"] == "supplement":
            source = db.get(SupplementRow, item["source_id"]) if db else None
            if (source is None or source.task_id != task.id or source.report_version != report_version
                    or supplement_hash(source.image_sha256, source.note) != item["source_version"]
                    or source.content_hash != item["source_version"] or source.note != item["excerpt"]):
                errors.append(f"SOURCE_MISMATCH:{evidence_id}")
        elif item["source_type"] in {"case", "sop"}:
            source = db.get(KnowledgeRow, item["source_id"]) if db else None
            if (source is None or source.tenant_id != task.tenant_id
                    or source.product_code != task.product_code
                    or source.source_type != item["source_type"]
                    or not current_document(source)
                    or document_version(source) != item["source_version"]
                    or sha256(source.content.encode()).hexdigest() != source.content_hash
                    or item["excerpt"] != source.content[:600]):
                errors.append(f"SOURCE_MISMATCH:{evidence_id}")
        else:
            errors.append(f"UNSUPPORTED_SOURCE:{evidence_id}")
    if not cited:
        errors.append("NO_EVIDENCE_REFERENCES")
    if report_version > 1 and not any(valid.get(key, {}).get("source_type") == "supplement" for key in cited):
        errors.append("SUPPLEMENT_NOT_REFERENCED")
    return errors
