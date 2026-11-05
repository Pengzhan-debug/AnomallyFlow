"""SQLAlchemy persistence schema for the first MVP development slice."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "username", name="uq_users_tenant_username"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    username: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SessionRow(Base):
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class BatchRow(Base):
    __tablename__ = "production_batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    product_code: Mapped[str] = mapped_column(String(64))
    equipment_id: Mapped[str] = mapped_column(String(64))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scenario_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class InspectionRow(Base):
    __tablename__ = "inspection_tasks"
    __table_args__ = (UniqueConstraint("tenant_id", "created_by", "idempotency_key", name="uq_inspection_idempotency"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("production_batches.id"), index=True)
    product_code: Mapped[str] = mapped_column(String(64))
    image_key: Mapped[str] = mapped_column(String(256))
    image_sha256: Mapped[str] = mapped_column(String(64))
    image_mime: Mapped[str] = mapped_column(String(32))
    image_width: Mapped[int] = mapped_column(Integer)
    image_height: Mapped[int] = mapped_column(Integer)
    remark: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    detection_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    anomaly_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    threshold_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detector_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    detection_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class VisualSampleRow(Base):
    __tablename__ = "visual_samples"
    __table_args__ = (
        UniqueConstraint("tenant_id", "image_sha256", name="uq_visual_sample_content"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    product_code: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(16))
    split: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(200))
    note: Mapped[str] = mapped_column(Text, default="")
    image_key: Mapped[str] = mapped_column(String(256))
    image_sha256: Mapped[str] = mapped_column(String(64))
    image_mime: Mapped[str] = mapped_column(String(32))
    image_width: Mapped[int] = mapped_column(Integer)
    image_height: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ReportRow(Base):
    __tablename__ = "investigation_reports"
    __table_args__ = (UniqueConstraint("task_id", "version", name="uq_report_task_version"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("inspection_tasks.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    report_json: Mapped[dict] = mapped_column(JSON)
    evidence_sufficient: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PipelineJobRow(Base):
    __tablename__ = "pipeline_jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("inspection_tasks.id"), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="READY", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    mode: Mapped[str] = mapped_column(String(16), default="mock", server_default="mock")
    report_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupplementRow(Base):
    __tablename__ = "inspection_supplements"
    __table_args__ = (
        UniqueConstraint("task_id", "report_version", name="uq_supplement_round"),
        UniqueConstraint("task_id", "created_by", "idempotency_key", name="uq_supplement_idempotency"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("inspection_tasks.id"), index=True)
    report_version: Mapped[int] = mapped_column(Integer)
    returned_approval_id: Mapped[str] = mapped_column(ForeignKey("approval_requests.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    image_key: Mapped[str] = mapped_column(String(256))
    image_sha256: Mapped[str] = mapped_column(String(64))
    image_mime: Mapped[str] = mapped_column(String(32))
    image_width: Mapped[int] = mapped_column(Integer)
    image_height: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GraphCheckpointRow(Base):
    __tablename__ = "graph_checkpoints"
    thread_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(200), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_type: Mapped[str] = mapped_column(String(32))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    meta_type: Mapped[str] = mapped_column(String(32))
    meta: Mapped[bytes] = mapped_column(LargeBinary)


class GraphWriteRow(Base):
    __tablename__ = "graph_writes"
    thread_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(200), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    index: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(200))
    data_type: Mapped[str] = mapped_column(String(32))
    data: Mapped[bytes] = mapped_column(LargeBinary)


class EvidenceRow(Base):
    __tablename__ = "evidence_items"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("inspection_tasks.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(80))
    source_version: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(160))
    excerpt: Mapped[str] = mapped_column(Text)
    validity: Mapped[str] = mapped_column(String(20))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class KnowledgeRow(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (UniqueConstraint("tenant_id", "source_key", "version", name="uq_knowledge_version"),)

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    source_key: Mapped[str] = mapped_column(String(64))
    source_type: Mapped[str] = mapped_column(String(16))
    product_code: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(160))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ToolExecutionRow(Base):
    __tablename__ = "tool_execution_logs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("inspection_tasks.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    report_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    arguments: Mapped[dict] = mapped_column(JSON)
    result_count: Mapped[int] = mapped_column(Integer)
    elapsed_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ApprovalRow(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("investigation_reports.id"), unique=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("inspection_tasks.id"), index=True)
    report_version: Mapped[int] = mapped_column(Integer)
    risk_level: Mapped[str] = mapped_column(String(16))
    stage: Mapped[str] = mapped_column(String(16), default="ENGINEER")
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    version: Mapped[int] = mapped_column(Integer, default=0)
    decisions_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkOrderRow(Base):
    __tablename__ = "work_orders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("inspection_tasks.id"), index=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approval_requests.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="OPEN")
    assignee_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkOrderEventRow(Base):
    __tablename__ = "work_order_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    work_order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ReinspectionRow(Base):
    __tablename__ = "reinspections"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    work_order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), index=True)
    order_version: Mapped[int] = mapped_column(Integer, default=0)
    image_key: Mapped[str] = mapped_column(String(256))
    result: Mapped[str] = mapped_column(String(20))
    inspector_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str] = mapped_column(Text)
    review_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    resource_type: Mapped[str] = mapped_column(String(40))
    resource_id: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
