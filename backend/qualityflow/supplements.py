"""Immutable supplementary material shared by mock and real investigation rounds."""
from hashlib import sha256

from sqlalchemy import select

from .db import ApprovalRow, ReportRow, SupplementRow


def evidence_prefix(task_id, report_version=1):
    return f"EV-{task_id}" if report_version == 1 else f"EV-{task_id}-V{report_version}"


def supplement_hash(image_hash, note):
    return sha256(f"{image_hash}|{note}".encode()).hexdigest()


def supplement_for(db, task_id, report_version):
    return db.scalar(select(SupplementRow).where(SupplementRow.task_id == task_id,
                                                SupplementRow.report_version == report_version))


def supplement_evidence(row):
    return {"id": f"{evidence_prefix(row.task_id, row.report_version)}-S", "source_type": "supplement",
            "source_id": row.id, "source_version": row.content_hash, "title": "质检员补证说明（待核验）",
            "excerpt": row.note, "validity": "CURRENT", "retrieved_at": row.created_at.isoformat()}


def return_context(db, row):
    approval = db.get(ApprovalRow, row.returned_approval_id)
    previous = db.get(ReportRow, approval.report_id)
    return {"previous_report_version": approval.report_version,
            "previous_report_id": previous.id,
            "previous_report_summary_untrusted": previous.report_json.get("defect_summary", ""),
            "previous_hypotheses_untrusted": previous.report_json.get("hypotheses", []),
            "previous_missing_evidence": previous.report_json.get("missing_evidence", []),
            "previous_risk_level": previous.report_json.get("risk_level", "MEDIUM"),
            "return_reason": approval.decisions_json[-1]["reason"] if approval.decisions_json else "",
            "supplement_note_untrusted": row.note,
            "supplement_image": {"available_for_human_review": True,
                                 "image_sha256": row.image_sha256, "machine_verified": False}}
