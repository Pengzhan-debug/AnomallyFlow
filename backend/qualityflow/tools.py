"""Tenant-scoped read-only tools and a small, reproducible local vector retriever."""
from __future__ import annotations

from collections import Counter
from datetime import timezone
from enum import StrEnum
from hashlib import sha256
from math import log, sqrt
import re
import json
from time import monotonic
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .db import AuditRow, BatchRow, InspectionRow, KnowledgeRow, ToolExecutionRow, utc_now
from .supplements import evidence_prefix


class ToolName(StrEnum):
    BATCH = "get_batch_info"
    HISTORY = "get_inspection_history"
    CASES = "search_quality_cases"
    SOP = "search_sop_documents"


def document_version(row: KnowledgeRow) -> str:
    return f"{row.version}:{row.content_hash[:16]}"


def history_excerpt(row: InspectionRow) -> str:
    return json.dumps({"task_id": row.id, "detection_status": row.detection_status,
                       "model_version": row.model_version}, ensure_ascii=False, sort_keys=True)


def history_version(row: InspectionRow) -> str:
    return sha256(history_excerpt(row).encode()).hexdigest()[:16]


def current_document(row: KnowledgeRow) -> bool:
    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    now = utc_now()
    return row.active and aware(row.valid_from) <= now and (row.valid_to is None or aware(row.valid_to) > now)


def terms(text: str) -> Counter:
    text = "".join(re.findall(r"[a-z0-9\u4e00-\u9fff]+", text.lower()))
    return Counter(text[index:index + size] for size in (2, 3)
                   for index in range(max(0, len(text) - size + 1)))


def rank_documents(query: str, documents: list[KnowledgeRow]) -> list[tuple[float, KnowledgeRow]]:
    """Character 2/3-gram TF-IDF cosine similarity; no semantic model is implied."""
    vectors = [terms(row.title + " " + row.content) for row in documents]
    query_terms = terms(query)
    df = Counter(term for vector in vectors for term in vector)
    def weights(vector):
        return {term: count * (log((len(vectors) + 1) / (df[term] + 1)) + 1)
                for term, count in vector.items()}
    q = weights(query_terms)
    q_norm = sqrt(sum(value * value for value in q.values()))
    ranked = []
    for row, vector in zip(documents, vectors):
        weighted = weights(vector)
        norm = sqrt(sum(value * value for value in weighted.values()))
        score = sum(value * weighted.get(term, 0) for term, value in q.items()) / (q_norm * norm) if q_norm and norm else 0
        if score >= 0.03:
            ranked.append((score, row))
    return sorted(ranked, key=lambda item: (-item[0], item[1].id))


class ReadOnlyTools:
    def __init__(self, db: Session, *, tenant_id: str, task_id: str, max_calls: int = 8, report_version: int = 1):
        self.db = db
        self.tenant_id = tenant_id
        self.task_id = task_id
        self.prefix = evidence_prefix(task_id, report_version)
        self.report_version = report_version
        self.max_calls = max_calls
        self.calls = 0
        self.started = monotonic()
        self.trace: list[dict] = []

    def run(self, name: ToolName, arguments: dict | None = None) -> dict:
        arguments = arguments or {}
        started = monotonic()
        self.calls += 1
        task = self.db.get(InspectionRow, self.task_id)
        error = None
        data, evidence = [], []
        if task is None or task.tenant_id != self.tenant_id:
            error = "FORBIDDEN"
        elif self.calls > self.max_calls or started - self.started >= 120:
            error = "TOOL_BUDGET_EXCEEDED"
        else:
            allowed = {ToolName.BATCH: {"batch_id"}, ToolName.HISTORY: {"limit"},
                       ToolName.CASES: {"query", "limit"}, ToolName.SOP: {"query", "limit"}}[name]
            if set(arguments) - allowed:
                error = "INVALID_TOOL_ARGUMENTS"
            elif name == ToolName.BATCH:
                if arguments.get("batch_id", task.batch_id) != task.batch_id:
                    error = "FORBIDDEN"
                else:
                    batch = self.db.get(BatchRow, task.batch_id)
                    if batch is None or batch.tenant_id != self.tenant_id:
                        error = "FORBIDDEN"
                    else:
                        data = [{"batch_id": batch.id, "product_code": batch.product_code,
                                 "equipment_id": batch.equipment_id}]
            else:
                limit = arguments.get("limit", 5)
                if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
                    error = "INVALID_TOOL_ARGUMENTS"
                elif name == ToolName.HISTORY:
                    rows = self.db.scalars(select(InspectionRow).where(
                        InspectionRow.tenant_id == self.tenant_id,
                        InspectionRow.product_code == task.product_code,
                        InspectionRow.id != task.id, InspectionRow.created_at <= task.created_at,
                        InspectionRow.detection_status.is_not(None))
                        .order_by(InspectionRow.created_at.desc(), InspectionRow.id).limit(limit)).all()
                    data = [{"task_id": row.id, "detection_status": row.detection_status,
                             "model_version": row.model_version} for row in rows]
                    evidence = [{"id": f"{self.prefix}-H-{sha256(row.id.encode()).hexdigest()[:8]}",
                        "source_type": "inspection_history", "source_id": row.id,
                        "source_version": history_version(row), "title": "历史检测任务 " + row.id,
                        "excerpt": history_excerpt(row), "validity": "HISTORICAL",
                        "retrieved_at": utc_now().isoformat()} for row in rows]
                else:
                    query = arguments.get("query", "")
                    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 256:
                        error = "INVALID_TOOL_ARGUMENTS"
                    else:
                        now = utc_now()
                        source_type = "case" if name == ToolName.CASES else "sop"
                        rows = self.db.scalars(select(KnowledgeRow).where(
                            KnowledgeRow.tenant_id == self.tenant_id,
                            KnowledgeRow.product_code == task.product_code,
                            KnowledgeRow.source_type == source_type, KnowledgeRow.active.is_(True),
                            KnowledgeRow.valid_from <= now,
                            or_(KnowledgeRow.valid_to.is_(None), KnowledgeRow.valid_to > now))
                            .order_by(KnowledgeRow.id).limit(200)).all()
                        for score, row in rank_documents(query.strip(), rows)[:limit]:
                            data.append({"source_id": row.id, "title": row.title, "score": round(score, 6)})
                            evidence.append({"id": f"{self.prefix}-{sha256(row.id.encode()).hexdigest()[:8]}",
                                "source_type": source_type, "source_id": row.id,
                                "source_version": document_version(row), "title": row.title,
                                "excerpt": row.content[:600], "validity": "CURRENT",
                                "retrieved_at": utc_now().isoformat()})
        status = "ERROR" if error else "OK" if data else "EMPTY"
        elapsed = max(0, int((monotonic() - started) * 1000))
        trace = {"tool_name": name.value, "status": status, "error_code": error,
                 "report_version": self.report_version, "result_count": len(data), "elapsed_ms": elapsed}
        self.trace.append(trace)
        # A denied cross-tenant request must not append to another tenant's task trace.
        if task is not None and task.tenant_id == self.tenant_id:
            self.db.add(ToolExecutionRow(id=str(uuid4()), tenant_id=self.tenant_id,
                task_id=self.task_id, tool_name=name.value, status=status, error_code=error,
                report_version=self.report_version,
                arguments=arguments, result_count=len(data), elapsed_ms=elapsed))
        if error:
            self.db.add(AuditRow(id=str(uuid4()), tenant_id=self.tenant_id, actor_id=None,
                action="READ_TOOL_ERROR", resource_type="inspection", resource_id=self.task_id,
                payload={"tool_name": name.value, "error_code": error}))
        return {"status": status, "data": data, "evidence": evidence,
                "error_code": error, "elapsed_ms": elapsed}
