"""Atomic deterministic inspection pipeline, invoked only by the durable worker."""
from datetime import datetime
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import ApprovalRow, AuditRow, BatchRow, EvidenceRow, InspectionRow, PipelineJobRow, ReportRow
from .investigation import mock_investigation, validate_evidence
from .detector import apply_detection


def execute_mock_pipeline(db: Session, task_id: str) -> None:
    task = db.scalar(select(InspectionRow).where(InspectionRow.id == task_id).with_for_update())
    if task is None or task.status != "PENDING":
        return
    job = db.scalar(select(PipelineJobRow).where(PipelineJobRow.task_id == task_id))
    version = job.report_version if job else 1
    batch = db.get(BatchRow, task.batch_id)
    if version > 1:
        status, error_code = "INVESTIGATING", None
    else:
        apply_detection(db, task, batch)
        status, error_code = task.status, task.error_code
    task.status = status
    task.error_code = error_code
    task.version += 1
    db.add(AuditRow(id=str(uuid4()), tenant_id=task.tenant_id, actor_id=None,
                    action="PIPELINE_STARTED" if version == 1 else "INVESTIGATION_RESTARTED", resource_type="inspection",
                    resource_id=task.id, payload={"detection_status": task.detection_status,
                                                  "task_status": status}))
    if status == "INVESTIGATING" and batch is not None:
        draft, sources = mock_investigation(task, batch, db, version)
        errors = validate_evidence(draft, sources, task, batch, db, version)
        if errors:
            task.status = "NEEDS_REVIEW"
            task.error_code = "EVIDENCE_VALIDATION_FAILED"
            db.add(AuditRow(id=str(uuid4()), tenant_id=task.tenant_id, actor_id=None,
                            action="EVIDENCE_VALIDATION_FAILED", resource_type="inspection",
                            resource_id=task.id, payload={"errors": errors}))
        else:
            publish_report(db, task, draft, sources, version)
        task.version += 1



def publish_report(db, task, draft, sources, version=1):
    report_id = f"RPT-{uuid4().hex[:12]}"
    approval_id = f"APR-{uuid4().hex[:12]}"
    report = ReportRow(id=report_id, task_id=task.id, version=version,
                       report_json=draft,
                       evidence_sufficient=draft["evidence_sufficient"])
    db.add(report)
    db.flush()  # insert the report before its approval FK on PostgreSQL
    for source in sources:
        db.add(EvidenceRow(id=source["id"], task_id=task.id,
                           tenant_id=task.tenant_id,
                           source_type=source["source_type"],
                           source_id=source["source_id"],
                           source_version=source["source_version"],
                           title=source["title"], excerpt=source["excerpt"],
                           validity=source["validity"],
                           retrieved_at=datetime.fromisoformat(source["retrieved_at"])))
    db.add(ApprovalRow(id=approval_id, report_id=report_id,
                       task_id=task.id, report_version=version,
                       risk_level=draft["risk_level"],
                       stage="ENGINEER", status="PENDING", version=0,
                       decisions_json=[]))
    task.status = "WAITING_APPROVAL"
    db.add(AuditRow(id=str(uuid4()), tenant_id=task.tenant_id, actor_id=None,
                    action="REPORT_CREATED", resource_type="report",
                    resource_id=report_id,
                    payload={"task_id": task.id, "approval_id": approval_id,
                             "evidence_count": len(sources),
                             "evidence_sufficient": draft["evidence_sufficient"]}))
    return report_id, approval_id


def execute_pipeline(db, task_id):
    from .db import PipelineJobRow
    job = db.scalar(select(PipelineJobRow).where(PipelineJobRow.task_id == task_id))
    if job is not None and job.mode == 'llm':
        from .agent_graph import run_agent_step
        return run_agent_step(db, task_id)
    return execute_mock_pipeline(db, task_id)
