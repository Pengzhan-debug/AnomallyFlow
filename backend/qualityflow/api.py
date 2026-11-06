"""First API slice: local accounts, batch lookup, image upload, and Mock detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .db import (ApprovalRow, AuditRow, Base, BatchRow, EvidenceRow, InspectionRow, PipelineJobRow, ReinspectionRow, ToolExecutionRow,
                 ReportRow, SessionRow, SupplementRow, UserRow, WorkOrderEventRow, WorkOrderRow, utc_now)
from .domain import (AccountService, ApprovalDecision, ApprovalRecord, ApprovalRequest,
                     ApprovalStage, ApprovalStatus, HandlingRecord, PolicyError,
                     Reinspection, ReinspectionDecision, ReinspectionResult, RiskLevel,
                     Role, WorkOrder, WorkOrderStatus)
from .config import database_url_from_env
from .tools import ReadOnlyTools, ToolName
from .supplements import supplement_hash
from .detector import DetectorConfigError, detector_config
from .samples import register_sample_routes
from .dsh_console import register_dsh_console_routes


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_SIDE = 10000
Image.MAX_IMAGE_PIXELS = 60_000_000


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class LoginInput(BaseModel):
    username: str
    password: str


class ToolInput(BaseModel):
    tool_name: ToolName
    arguments: dict = Field(default_factory=dict)


class PasswordInput(BaseModel):
    old_password: str
    new_password: str = Field(min_length=12)


class CreateUserInput(BaseModel):
    username: str
    display_name: str
    role: Role
    active: bool = True
    reason: str


class UpdateUserInput(BaseModel):
    role: Role
    active: bool
    reason: str


class ApprovalInput(BaseModel):
    decision: ApprovalDecision
    reason: str = ""
    expected_version: int = Field(ge=0)
    expected_report_version: int = Field(ge=1)


class HandlingInput(BaseModel):
    action: str
    expected_version: int = Field(ge=0)


class StartOrderInput(BaseModel):
    reason: str
    expected_version: int = Field(ge=0)


class ReviewReinspectionInput(BaseModel):
    decision: ReinspectionDecision
    reason: str
    expected_version: int = Field(ge=0)


def error_response(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "error_code": code,
        "message": message,
        "details": details or {},
        "trace_id": f"trc-{uuid4().hex[:16]}",
    })


def create_app(
    *,
    database_url: str | None = None,
    storage_root: str | Path | None = None,
    create_schema: bool = False,
    tenant_id: str | None = None,
    agent_mode: str | None = None,
    detector_mode: str | None = None,
) -> FastAPI:
    database_url = database_url or database_url_from_env()
    storage_root = Path(storage_root or os.environ.get("STORAGE_ROOT", "./storage"))
    tenant_id = tenant_id or os.environ.get("DEFAULT_TENANT_ID", "demo-factory")
    connect_args = {"check_same_thread": False} if str(database_url).startswith("sqlite:") else {}
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    if create_schema:
        Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    app = FastAPI(title="QualityFlow AI 2.0 API", version="0.1.0")
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.storage_root = storage_root
    app.state.tenant_id = tenant_id
    app.state.agent_mode = agent_mode or os.environ.get("AGENT_MODE", "mock")
    if app.state.agent_mode not in {"mock", "llm"}:
        raise ValueError("AGENT_MODE must be mock or llm")
    app.state.detector_mode = detector_mode or os.environ.get("DETECTOR_MODE", "mock")
    if app.state.detector_mode not in {"mock", "http"}:
        raise ValueError("DETECTOR_MODE must be mock or http")

    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, exc: APIError):
        return error_response(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        safe_fields = [".".join(map(str, e["loc"])) for e in exc.errors()]
        return error_response(422, "VALIDATION_ERROR", "请求字段不符合要求", {"fields": safe_fields})

    def db_session(request: Request):
        with request.app.state.session_factory() as db:
            yield db

    def current_user_allow_reset(
        authorization: str | None = Header(default=None),
        db: Session = Depends(db_session),
    ) -> UserRow:
        if not authorization or not authorization.startswith("Bearer "):
            raise APIError(401, "UNAUTHENTICATED", "请先登录")
        token_hash = sha256(authorization[7:].encode("utf-8")).hexdigest()
        auth_session = db.get(SessionRow, token_hash)
        if auth_session is None:
            raise APIError(401, "UNAUTHENTICATED", "登录状态无效")
        expires_at = auth_session.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= utc_now():
            raise APIError(401, "SESSION_EXPIRED", "登录状态已过期")
        user = db.get(UserRow, auth_session.user_id)
        if user is None or not user.active:
            raise APIError(401, "UNAUTHENTICATED", "账户不可用")
        return user

    def current_user(user: UserRow = Depends(current_user_allow_reset)) -> UserRow:
        if user.must_change_password:
            raise APIError(403, "PASSWORD_CHANGE_REQUIRED", "首次登录请修改临时密码")
        return user

    def require_role(user: UserRow, allowed: set[Role], db: Session, action: str) -> None:
        if Role(user.role) not in allowed:
            db.add(AuditRow(id=str(uuid4()), tenant_id=user.tenant_id, actor_id=user.id,
                            action="PERMISSION_DENIED", resource_type="api", resource_id=action,
                            payload={"role": user.role}))
            db.commit()
            raise APIError(403, "FORBIDDEN", "当前角色无权执行此操作")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/api/v1/auth/login")
    def login(payload: LoginInput, db: Session = Depends(db_session)):
        username = payload.username.strip().lower()
        user = db.scalar(select(UserRow).where(UserRow.tenant_id == tenant_id,
                                               UserRow.username == username))
        if user is None or not user.active or not AccountService.verify_password(payload.password, user.password_hash):
            raise APIError(401, "INVALID_CREDENTIALS", "账号或密码不正确")
        token = token_urlsafe(32)
        db.add(SessionRow(token_hash=sha256(token.encode("utf-8")).hexdigest(),
                          user_id=user.id, expires_at=utc_now() + timedelta(hours=8)))
        db.commit()
        return {"access_token": token, "token_type": "bearer", "expires_in": 28800,
                "must_change_password": user.must_change_password, "role": user.role}

    @app.post("/api/v1/auth/change-password")
    def change_password(payload: PasswordInput,
                        user: UserRow = Depends(current_user_allow_reset),
                        db: Session = Depends(db_session)):
        if not AccountService.verify_password(payload.old_password, user.password_hash):
            raise APIError(401, "INVALID_CREDENTIALS", "账号或密码不正确")
        if payload.new_password == payload.old_password:
            raise APIError(422, "WEAK_PASSWORD", "新密码不能与旧密码相同")
        user.password_hash = AccountService._hash_password(payload.new_password)
        user.must_change_password = False
        db.add(AuditRow(id=str(uuid4()), tenant_id=user.tenant_id, actor_id=user.id,
                        action="CHANGE_PASSWORD", resource_type="user", resource_id=user.id,
                        payload={}))
        db.commit()
        return {"status": "changed"}

    @app.post("/api/v1/users", status_code=201)
    def create_user(payload: CreateUserInput, user: UserRow = Depends(current_user),
                    db: Session = Depends(db_session)):
        require_role(user, {Role.ADMIN}, db, "CREATE_USER")
        username = payload.username.strip().lower()
        if not username or not payload.display_name.strip() or not payload.reason.strip():
            raise APIError(422, "VALIDATION_ERROR", "账号、姓名和创建原因不能为空")
        if db.scalar(select(UserRow.id).where(UserRow.tenant_id == user.tenant_id,
                                              UserRow.username == username)):
            raise APIError(409, "ACCOUNT_EXISTS", "该租户中的账号已存在")
        temporary_password = token_urlsafe(24)
        created = UserRow(id=str(uuid4()), tenant_id=user.tenant_id, username=username,
                          display_name=payload.display_name.strip(), role=payload.role.value,
                          password_hash=AccountService._hash_password(temporary_password),
                          active=payload.active, must_change_password=True)
        db.add(created)
        db.add(AuditRow(id=str(uuid4()), tenant_id=user.tenant_id, actor_id=user.id,
                        action="CREATE_USER", resource_type="user", resource_id=created.id,
                        payload={"role": created.role, "active": created.active,
                                 "reason": payload.reason.strip()}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise APIError(409, "ACCOUNT_EXISTS", "该租户中的账号已存在") from None
        return {"id": created.id, "username": username, "role": created.role,
                "active": created.active,
                "temporary_password": temporary_password, "must_change_password": True}

    @app.get("/api/v1/users")
    def list_users(user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        require_role(user, {Role.ADMIN}, db, "LIST_USERS")
        users = db.scalars(select(UserRow).where(UserRow.tenant_id == user.tenant_id)
                           .order_by(UserRow.username)).all()
        return {"items": [{"id": item.id, "username": item.username,
                           "display_name": item.display_name, "role": item.role,
                           "active": item.active,
                           "must_change_password": item.must_change_password,
                           "created_at": item.created_at.isoformat()} for item in users]}

    @app.patch("/api/v1/users/{user_id}")
    def update_user(user_id: str, payload: UpdateUserInput,
                    user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        require_role(user, {Role.ADMIN}, db, "UPDATE_USER")
        target = db.get(UserRow, user_id)
        if target is None or target.tenant_id != user.tenant_id:
            raise APIError(404, "NOT_FOUND", "账户不存在")
        if not payload.reason.strip():
            raise APIError(422, "REASON_REQUIRED", "权限变更必须填写原因")
        if target.id == user.id and (not payload.active or payload.role != Role.ADMIN):
            raise APIError(409, "SELF_LOCKOUT", "不能禁用或移除当前管理员自身权限")
        if target.role == Role.ADMIN.value and target.active and (not payload.active or payload.role != Role.ADMIN):
            another_admin = db.scalar(select(UserRow.id).where(
                UserRow.tenant_id == user.tenant_id, UserRow.id != target.id,
                UserRow.role == Role.ADMIN.value, UserRow.active.is_(True)).limit(1))
            if another_admin is None:
                raise APIError(409, "LAST_ADMIN", "不能停用或移除本租户最后一位管理员")
        before = {"role": target.role, "active": target.active}
        target.role, target.active = payload.role.value, payload.active
        db.add(AuditRow(id=str(uuid4()), tenant_id=user.tenant_id, actor_id=user.id,
                        action="UPDATE_USER", resource_type="user", resource_id=target.id,
                        payload={"before": before, "after": {"role": target.role, "active": target.active},
                                 "reason": payload.reason.strip()}))
        db.commit()
        return {"id": target.id, "role": target.role, "active": target.active}

    @app.get("/api/v1/batches")
    def list_batches(product_code: str | None = None, user: UserRow = Depends(current_user),
                     db: Session = Depends(db_session)):
        query = select(BatchRow).where(BatchRow.tenant_id == user.tenant_id)
        if product_code:
            query = query.where(BatchRow.product_code == product_code)
        batches = db.scalars(query.order_by(BatchRow.id).limit(100)).all()
        return {"items": [{"id": b.id, "product_code": b.product_code,
                            "equipment_id": b.equipment_id} for b in batches]}

    def inspect_image(data: bytes, declared_mime: str | None) -> tuple[str, int, int]:
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise APIError(422, "INVALID_IMAGE", "图像为空或超过 20 MB")
        if declared_mime not in {"image/jpeg", "image/png"}:
            raise APIError(422, "INVALID_IMAGE_TYPE", "仅支持 JPG 和 PNG 图像")
        try:
            with Image.open(BytesIO(data)) as opened:
                image_format = opened.format
                opened.verify()
            with Image.open(BytesIO(data)) as opened:
                width, height = opened.size
                opened.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
            raise APIError(422, "INVALID_IMAGE", "图像无法完整解码") from None
        actual_mime = {"JPEG": "image/jpeg", "PNG": "image/png"}.get(image_format)
        if actual_mime != declared_mime or width < 1 or height < 1 or max(width, height) > MAX_IMAGE_SIDE:
            raise APIError(422, "INVALID_IMAGE", "图像类型或尺寸不符合要求")
        return actual_mime, width, height

    @app.post("/api/v1/inspections", status_code=202)
    async def create_inspection(
        request: Request,
        batch_id: str = Form(...), product_code: str = Form(...),
        image: UploadFile = File(...), remark: str = Form(default=""),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        user: UserRow = Depends(current_user), db: Session = Depends(db_session),
    ):
        require_role(user, {Role.INSPECTOR}, db, "CREATE_INSPECTION")
        if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
            raise APIError(422, "IDEMPOTENCY_KEY_REQUIRED", "请提供 8 至 128 位 Idempotency-Key")
        batch = db.get(BatchRow, batch_id)
        if batch is None or batch.tenant_id != user.tenant_id or batch.product_code != product_code:
            raise APIError(422, "INVALID_BATCH", "批次不存在或与产品不匹配")
        data = await image.read(MAX_IMAGE_BYTES + 1)
        mime, width, height = inspect_image(data, image.content_type)
        image_hash = sha256(data).hexdigest()
        request_hash = sha256(f"{batch_id}|{product_code}|{image_hash}|{remark}".encode()).hexdigest()
        existing = db.scalar(select(InspectionRow).where(
            InspectionRow.tenant_id == user.tenant_id,
            InspectionRow.created_by == user.id,
            InspectionRow.idempotency_key == idempotency_key))
        if existing is not None:
            if existing.request_hash != request_hash:
                raise APIError(409, "IDEMPOTENCY_KEY_REUSED", "此幂等键已用于不同请求")
            return {"task_id": existing.id, "status": existing.status, "replayed": True}
        try:
            profile = detector_config(product_code, request.app.state.detector_mode)
        except DetectorConfigError:
            raise APIError(503, "DETECTOR_NOT_CONFIGURED", "当前产品尚未配置可用的检测服务和阈值版本") from None
        image_key = f"originals/{image_hash[:2]}/{image_hash}"
        target = storage_root / image_key
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        task = InspectionRow(id=f"QI-{uuid4().hex[:12]}", tenant_id=user.tenant_id,
                             created_by=user.id, batch_id=batch_id, product_code=product_code,
                             image_key=image_key, image_sha256=image_hash, image_mime=mime,
                             image_width=width, image_height=height, remark=remark,
                             status="PENDING", detector_config=profile, idempotency_key=idempotency_key,
                             request_hash=request_hash)
        db.add(task)
        db.add(AuditRow(id=str(uuid4()), tenant_id=user.tenant_id, actor_id=user.id,
                        action="CREATE_INSPECTION", resource_type="inspection",
                        resource_id=task.id, payload={"batch_id": batch_id,
                                                      "product_code": product_code}))
        try:
            db.flush()
            db.add(PipelineJobRow(id=str(uuid4()), task_id=task.id, status="READY", attempts=0,
                                  mode=request.app.state.agent_mode))
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(select(InspectionRow).where(
                InspectionRow.tenant_id == user.tenant_id,
                InspectionRow.created_by == user.id,
                InspectionRow.idempotency_key == idempotency_key))
            if existing and existing.request_hash == request_hash:
                return {"task_id": existing.id, "status": existing.status, "replayed": True}
            raise APIError(409, "IDEMPOTENCY_KEY_REUSED", "此幂等键已用于不同请求") from None
        return {"task_id": task.id, "status": "PENDING", "replayed": False}

    def visible_task(db: Session, task_id: str, user: UserRow) -> InspectionRow:
        task = db.get(InspectionRow, task_id)
        if task is None or task.tenant_id != user.tenant_id:
            raise APIError(404, "NOT_FOUND", "任务不存在")
        if Role(user.role) == Role.INSPECTOR and task.created_by != user.id:
            raise APIError(404, "NOT_FOUND", "任务不存在")
        return task

    def task_payload(task: InspectionRow, db: Session) -> dict:
        job = db.scalar(select(PipelineJobRow).where(PipelineJobRow.task_id == task.id))
        report = db.scalar(select(ReportRow).where(ReportRow.task_id == task.id)
                           .order_by(ReportRow.version.desc()).limit(1))
        approval = db.scalar(select(ApprovalRow).where(ApprovalRow.report_id == report.id)) if report else None
        order = db.scalar(select(WorkOrderRow).where(WorkOrderRow.approval_id == approval.id)) if approval else None
        return {"task_id": task.id, "batch_id": task.batch_id,
                "product_code": task.product_code, "status": task.status,
                "detection_status": task.detection_status,
                "anomaly_score": task.anomaly_score,
                "model_version": task.model_version,
                "threshold_version": task.threshold_version,
                "detection": {k: v for k, v in task.detection_json.items() if k != "anomaly_map_key"} if task.detection_json else None,
                "has_anomaly_map": bool(task.detection_json and task.detection_json.get("anomaly_map_key")),
                "detector_mode": (task.detector_config or {}).get("mode", "mock"),
                "error_code": task.error_code, "version": task.version,
                "current_report_version": job.report_version if job else (report.version if report else 1),
                "latest_report_version": report.version if report else None,
                "return_reason": approval.decisions_json[-1]["reason"] if approval and approval.status == "RETURNED" and approval.decisions_json else None,
                "execution": {"status": job.status, "mode": job.mode, "attempts": job.attempts,
                              "last_error_code": job.last_error_code} if job else None,
                "report_id": report.id if report else None,
                "approval_id": approval.id if approval else None,
                "work_order_id": order.id if order else None,
                "created_at": task.created_at.isoformat()}

    @app.get("/api/v1/inspections")
    def list_inspections(status: str | None = None, batch_id: str | None = None,
                         limit: int = 20, user: UserRow = Depends(current_user),
                         db: Session = Depends(db_session)):
        if not 1 <= limit <= 100:
            raise APIError(422, "VALIDATION_ERROR", "limit 必须在 1 到 100 之间")
        query = select(InspectionRow).where(InspectionRow.tenant_id == user.tenant_id)
        if Role(user.role) == Role.INSPECTOR:
            query = query.where(InspectionRow.created_by == user.id)
        if status:
            query = query.where(InspectionRow.status == status)
        if batch_id:
            query = query.where(InspectionRow.batch_id == batch_id)
        tasks = db.scalars(query.order_by(InspectionRow.created_at.desc()).limit(limit)).all()
        return {"items": [task_payload(t, db) for t in tasks], "next_cursor": None}

    @app.get("/api/v1/inspections/{task_id}")
    def get_inspection(task_id: str, user: UserRow = Depends(current_user),
                       db: Session = Depends(db_session)):
        return task_payload(visible_task(db, task_id, user), db)

    def private_image(image_key, mime):
        target = (storage_root / image_key).resolve()
        if not target.is_relative_to(storage_root.resolve()) or not target.is_file():
            raise APIError(404, "IMAGE_NOT_FOUND", "图像不可用")
        return FileResponse(target, media_type=mime, headers={"Cache-Control": "private, no-store"})

    register_sample_routes(app, db_session=db_session, current_user=current_user, require_role=require_role,
        inspect_image=inspect_image, private_image=private_image, storage_root=storage_root, api_error=APIError)

    @app.get("/api/v1/inspections/{task_id}/anomaly-map")
    def anomaly_map(task_id: str, user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        task = visible_task(db, task_id, user)
        key = (task.detection_json or {}).get("anomaly_map_key")
        if not key:
            raise APIError(404, "ANOMALY_MAP_NOT_FOUND", "本次检测未提供异常定位图")
        return private_image(key, "image/png")

    @app.get("/api/v1/inspections/{task_id}/image")
    def original_image(task_id: str, user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        task = visible_task(db, task_id, user)
        return private_image(task.image_key, task.image_mime)

    @app.get("/api/v1/inspections/{task_id}/report")
    def get_report(task_id: str, version: int | None = None,
                   user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        visible_task(db, task_id, user)
        query = select(ReportRow).where(ReportRow.task_id == task_id)
        if version is not None:
            query = query.where(ReportRow.version == version)
        else:
            query = query.order_by(ReportRow.version.desc())
        report = db.scalar(query.limit(1))
        if report is None:
            raise APIError(404, "REPORT_NOT_FOUND", "调查报告尚未生成")
        approval = db.scalar(select(ApprovalRow).where(ApprovalRow.report_id == report.id))
        return {"report_id": report.id, "task_id": task_id,
                "report_version": report.version, **report.report_json,
                "approval_id": approval.id if approval else None,
                "approval_status": approval.status if approval else None}

    @app.get("/api/v1/inspections/{task_id}/reports")
    def report_versions(task_id: str, user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        visible_task(db, task_id, user)
        reports = db.scalars(select(ReportRow).where(ReportRow.task_id == task_id).order_by(ReportRow.version.desc())).all()
        items = []
        for report in reports:
            approval = db.scalar(select(ApprovalRow).where(ApprovalRow.report_id == report.id))
            items.append({"report_id": report.id, "report_version": report.version,
                "created_at": report.created_at.isoformat(), "approval_id": approval.id if approval else None,
                "approval_status": approval.status if approval else None,
                "decisions": approval.decisions_json if approval else []})
        return {"items": items}

    def supplement_payload(row: SupplementRow, replayed=False):
        return {"supplement_id": row.id, "task_id": row.task_id, "report_version": row.report_version,
                "returned_approval_id": row.returned_approval_id, "note": row.note,
                "created_at": row.created_at.isoformat(), "image_sha256": row.image_sha256,
                "image_width": row.image_width, "image_height": row.image_height, "replayed": replayed}

    @app.get("/api/v1/inspections/{task_id}/supplements")
    def list_supplements(task_id: str, user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        visible_task(db, task_id, user)
        rows = db.scalars(select(SupplementRow).where(SupplementRow.task_id == task_id)
                          .order_by(SupplementRow.report_version)).all()
        return {"items": [supplement_payload(row) for row in rows]}

    @app.get("/api/v1/inspections/{task_id}/supplements/{supplement_id}/image")
    def get_supplement_image(task_id: str, supplement_id: str,
                             user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        visible_task(db, task_id, user)
        row = db.get(SupplementRow, supplement_id)
        if row is None or row.task_id != task_id:
            raise APIError(404, "NOT_FOUND", "补证材料不存在")
        return private_image(row.image_key, row.image_mime)

    @app.post("/api/v1/inspections/{task_id}/supplements", status_code=202)
    async def submit_supplement(task_id: str, image: UploadFile = File(...), note: str = Form(..., max_length=4000),
                                expected_version: int = Form(...), expected_report_version: int = Form(...),
                                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                                user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        require_role(user, {Role.INSPECTOR}, db, "SUBMIT_SUPPLEMENT")
        task = visible_task(db, task_id, user)
        if not idempotency_key or not 8 <= len(idempotency_key) <= 128 or not note.strip():
            raise APIError(422, "VALIDATION_ERROR", "请填写补证说明和有效的幂等键")
        note = note.strip()
        data = await image.read(MAX_IMAGE_BYTES + 1)
        mime, width, height = inspect_image(data, image.content_type)
        image_hash = sha256(data).hexdigest()
        fingerprint = sha256(f"{expected_report_version}|{image_hash}|{note}".encode()).hexdigest()
        job = db.scalar(select(PipelineJobRow).where(PipelineJobRow.task_id == task_id).with_for_update())
        db.refresh(task)
        replay_query = select(SupplementRow).where(SupplementRow.task_id == task_id,
            SupplementRow.created_by == user.id, SupplementRow.idempotency_key == idempotency_key)
        existing = db.scalar(replay_query)
        if existing:
            if existing.request_hash != fingerprint:
                raise APIError(409, "IDEMPOTENCY_KEY_REUSED", "此幂等键已用于不同补证内容")
            return supplement_payload(existing, True)
        latest = db.scalar(select(ReportRow).where(ReportRow.task_id == task_id).order_by(ReportRow.version.desc()).limit(1))
        approval = db.scalar(select(ApprovalRow).where(ApprovalRow.report_id == latest.id)) if latest else None
        if (job is None or latest is None or approval is None or approval.status != "RETURNED"
                or task.status != "NEEDS_REVIEW" or task.error_code != "MORE_EVIDENCE_REQUIRED"
                or job.report_version != latest.version):
            raise APIError(409, "SUPPLEMENT_NOT_ALLOWED", "只有当前报告退回补证后才可提交新材料")
        if latest.version != expected_report_version or task.version != expected_version:
            raise APIError(409, "REPORT_VERSION_CONFLICT", "任务或报告版本已变化，请重新读取")
        if job.mode == "llm":
            from .agent_graph import finish_returned_checkpoint
            finish_returned_checkpoint(db, task_id)
        updated = db.execute(update(InspectionRow).where(InspectionRow.id == task_id,
            InspectionRow.version == expected_version, InspectionRow.status == "NEEDS_REVIEW")
            .values(status="PENDING", error_code=None, version=expected_version + 1)
            .execution_options(synchronize_session=False))
        if updated.rowcount != 1:
            db.rollback()
            existing = db.scalar(replay_query)
            if existing and existing.request_hash == fingerprint:
                return supplement_payload(existing, True)
            raise APIError(409, "REPORT_VERSION_CONFLICT", "任务状态已变化")
        new_version = latest.version + 1
        identifier = f"SUP-{uuid4().hex[:12]}"
        image_key = f"supplements/{task_id}/{identifier}"
        row = SupplementRow(id=identifier, task_id=task_id, report_version=new_version,
            returned_approval_id=approval.id, created_by=user.id, image_key=image_key,
            image_sha256=image_hash, image_mime=mime, image_width=width, image_height=height,
            note=note, content_hash=supplement_hash(image_hash, note), idempotency_key=idempotency_key,
            request_hash=fingerprint)
        db.add(row)
        job.report_version, job.status, job.attempts = new_version, "READY", 0
        job.available_at, job.finished_at, job.lease_token, job.lease_until = utc_now(), None, None, None
        job.last_error_code = None
        db.add(AuditRow(id=str(uuid4()), tenant_id=user.tenant_id, actor_id=user.id,
            action="SUPPLEMENT_SUBMITTED", resource_type="supplement", resource_id=identifier,
            payload={"task_id": task_id, "report_version": new_version, "returned_approval_id": approval.id}))
        target = (storage_root / image_key).resolve()
        if not target.is_relative_to(storage_root.resolve()):
            raise APIError(422, "INVALID_IMAGE", "图像保存路径不合法")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            db.commit()
        except Exception:
            db.rollback()
            target.unlink(missing_ok=True)
            raise
        return supplement_payload(row)

    @app.post("/api/v1/inspections/{task_id}/tools")
    def run_read_tool(task_id: str, payload: ToolInput, user: UserRow = Depends(current_user),
                      db: Session = Depends(db_session)):
        require_role(user, {Role.ENGINEER, Role.MANAGER}, db, "RUN_READ_TOOL")
        visible_task(db, task_id, user)
        job = db.scalar(select(PipelineJobRow).where(PipelineJobRow.task_id == task_id))
        result = ReadOnlyTools(db, tenant_id=user.tenant_id, task_id=task_id,
                               report_version=job.report_version if job else 1).run(
            payload.tool_name, payload.arguments)
        db.commit()
        return result

    @app.get("/api/v1/inspections/{task_id}/tools")
    def tool_trace(task_id: str, version: int | None = None,
                   user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        visible_task(db, task_id, user)
        query = select(ToolExecutionRow).where(ToolExecutionRow.task_id == task_id,
                                              ToolExecutionRow.tenant_id == user.tenant_id)
        if version is not None:
            query = query.where(ToolExecutionRow.report_version == version)
        rows = db.scalars(query.order_by(ToolExecutionRow.created_at, ToolExecutionRow.id).limit(100)).all()
        return {"items": [{"id": row.id, "report_version": row.report_version, "tool_name": row.tool_name, "status": row.status,
                           "error_code": row.error_code, "elapsed_ms": row.elapsed_ms,
                           "result_count": row.result_count, "created_at": row.created_at.isoformat()}
                          for row in rows]}

    @app.get("/api/v1/inspections/{task_id}/evidence/{evidence_id}")
    def get_evidence(task_id: str, evidence_id: str,
                     user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        visible_task(db, task_id, user)
        evidence = db.get(EvidenceRow, evidence_id)
        if evidence is None or evidence.task_id != task_id or evidence.tenant_id != user.tenant_id:
            raise APIError(404, "EVIDENCE_NOT_FOUND", "证据不存在")
        return {"evidence_id": evidence.id, "task_id": task_id,
                "source_type": evidence.source_type, "source_id": evidence.source_id,
                "source_version": evidence.source_version, "title": evidence.title,
                "excerpt": evidence.excerpt, "validity": evidence.validity,
                "retrieved_at": evidence.retrieved_at.isoformat()}

    def policy_error(exc: PolicyError) -> APIError:
        status = 403 if exc.code == "FORBIDDEN" else 409 if exc.code.endswith("CONFLICT") or exc.code in {
            "SAME_APPROVER", "DECISION_CONFLICT", "APPROVAL_FINALIZED", "INVALID_WORK_ORDER_STATE",
            "REINSPECTION_PENDING_REVIEW", "REINSPECTION_ALREADY_REVIEWED", "UNCERTAIN_CANNOT_CLOSE",
            "RESULT_CONFLICT"} else 422
        return APIError(status, exc.code, str(exc))

    def approval_for_tenant(db: Session, approval_id: str, user: UserRow) -> tuple[ApprovalRow, InspectionRow]:
        row = db.get(ApprovalRow, approval_id)
        task = db.get(InspectionRow, row.task_id) if row else None
        if row is None or task is None or task.tenant_id != user.tenant_id:
            raise APIError(404, "NOT_FOUND", "审批请求不存在")
        return row, task

    def approval_payload(row: ApprovalRow, work_order_id: str | None = None) -> dict:
        return {"approval_id": row.id, "report_id": row.report_id,
                "report_version": row.report_version, "risk_level": row.risk_level,
                "stage": row.stage, "status": row.status, "version": row.version,
                "decisions": row.decisions_json, "work_order_id": work_order_id}

    @app.get("/api/v1/approvals/{approval_id}")
    def get_approval(approval_id: str, user: UserRow = Depends(current_user),
                     db: Session = Depends(db_session)):
        require_role(user, {Role.ENGINEER, Role.MANAGER, Role.AUDITOR}, db, "GET_APPROVAL")
        row, _ = approval_for_tenant(db, approval_id, user)
        order = db.scalar(select(WorkOrderRow).where(WorkOrderRow.approval_id == row.id))
        return approval_payload(row, order.id if order else None)

    @app.post("/api/v1/approvals/{approval_id}/decision")
    def decide_approval(approval_id: str, payload: ApprovalInput,
                        user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        require_role(user, {Role.ENGINEER, Role.MANAGER}, db, "DECIDE_APPROVAL")
        row, task = approval_for_tenant(db, approval_id, user)
        job = db.scalar(select(PipelineJobRow).where(PipelineJobRow.task_id == task.id).with_for_update())
        db.refresh(row)
        db.refresh(task)
        if job is not None and job.report_version != row.report_version:
            raise APIError(409, "REPORT_VERSION_CONFLICT", "该报告已被新一轮调查替代")
        latest = db.scalar(select(ReportRow).where(ReportRow.task_id == task.id)
                           .order_by(ReportRow.version.desc()).limit(1))
        if latest is None or latest.id != row.report_id or latest.version != row.report_version:
            raise APIError(409, "REPORT_VERSION_CONFLICT", "当前审批绑定的报告不是最新版本")
        records = [ApprovalRecord(actor_id=r["actor_id"], actor_role=Role(r["actor_role"]),
                                  stage=ApprovalStage(r["stage"]),
                                  decision=ApprovalDecision(r["decision"]), reason=r["reason"],
                                  report_version=r["report_version"]) for r in row.decisions_json]
        aggregate = ApprovalRequest(id=row.id, report_id=row.report_id,
                                    report_version=row.report_version, risk_level=RiskLevel(row.risk_level),
                                    stage=ApprovalStage(row.stage), status=ApprovalStatus(row.status),
                                    version=row.version, decisions=records)
        old_count = len(aggregate.decisions)
        try:
            record = aggregate.decide(actor_id=user.id, actor_role=Role(user.role),
                                      decision=payload.decision, reason=payload.reason,
                                      expected_version=payload.expected_version,
                                      expected_report_version=payload.expected_report_version)
        except PolicyError as exc:
            raise policy_error(exc) from None
        if len(aggregate.decisions) == old_count:
            existing = db.scalar(select(WorkOrderRow).where(WorkOrderRow.approval_id == row.id))
            return approval_payload(row, existing.id if existing else None)
        decision_data = {"actor_id": record.actor_id, "actor_role": record.actor_role.value,
                         "stage": record.stage.value, "decision": record.decision.value,
                         "reason": record.reason, "report_version": record.report_version,
                         "decided_at": utc_now().isoformat()}
        result = db.execute(update(ApprovalRow).where(ApprovalRow.id == row.id,
                    ApprovalRow.version == payload.expected_version,
                    ApprovalRow.status == ApprovalStatus.PENDING.value)
                    .values(stage=aggregate.stage.value, status=aggregate.status.value,
                            version=aggregate.version,
                            decisions_json=[*row.decisions_json, decision_data]))
        if result.rowcount != 1:
            db.rollback()
            raise APIError(409, "APPROVAL_VERSION_CONFLICT", "审批状态已变化，请重新读取")
        order_id = None
        if aggregate.can_create_order:
            order_id = f"WO-{uuid4().hex[:12]}"
            db.add(WorkOrderRow(id=order_id, task_id=task.id, approval_id=row.id,
                                idempotency_key=f"approval:{row.id}", status="OPEN"))
            try:
                db.flush()  # insert the order before its first event FK
            except IntegrityError:
                db.rollback()
                raise APIError(409, "APPROVAL_VERSION_CONFLICT", "审批或工单状态已变化") from None
            db.add(WorkOrderEventRow(id=str(uuid4()), work_order_id=order_id,
                                     event_type="CREATED", actor_id=user.id,
                                     payload={"approval_id": row.id, "report_version": row.report_version}))
            task.status = "ORDER_CREATED"
            task.version += 1
        elif aggregate.status == ApprovalStatus.REJECTED:
            task.status = "REJECTED"
            task.version += 1
        elif aggregate.status == ApprovalStatus.RETURNED:
            task.status = "NEEDS_REVIEW"
            task.error_code = "MORE_EVIDENCE_REQUIRED"
            task.version += 1
        if job is not None and job.mode == "llm" and aggregate.status != ApprovalStatus.PENDING:
            job.status, job.attempts, job.available_at = "READY", 0, utc_now()
            job.finished_at = None
        db.add(AuditRow(id=str(uuid4()), tenant_id=user.tenant_id, actor_id=user.id,
                        action="APPROVAL_DECISION", resource_type="approval", resource_id=row.id,
                        payload=decision_data))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise APIError(409, "APPROVAL_VERSION_CONFLICT", "审批或工单状态已变化") from None
        db.refresh(row)
        return approval_payload(row, order_id)

    def order_for_tenant(db: Session, order_id: str, user: UserRow) -> WorkOrderRow:
        row = db.get(WorkOrderRow, order_id)
        task = db.get(InspectionRow, row.task_id) if row else None
        if row is None or task is None or task.tenant_id != user.tenant_id:
            raise APIError(404, "NOT_FOUND", "工单不存在")
        if Role(user.role) == Role.INSPECTOR and task.created_by != user.id:
            raise APIError(404, "NOT_FOUND", "工单不存在")
        return row

    def order_payload(db: Session, row: WorkOrderRow) -> dict:
        reinspections = db.scalars(select(ReinspectionRow).where(
            ReinspectionRow.work_order_id == row.id).order_by(ReinspectionRow.created_at,
                                                               ReinspectionRow.id)).all()
        return {"work_order_id": row.id, "task_id": row.task_id,
                "approval_id": row.approval_id, "status": row.status,
                "assignee_id": row.assignee_id, "version": row.version,
                "reinspections": [{"id": r.id, "order_version": r.order_version,
                                   "created_at": r.created_at.isoformat(),
                                   "result": r.result, "note": r.note,
                                   "status": "RECAPTURE_REQUIRED" if r.review_decision == "REQUEST_RECAPTURE" else
                                             "REVIEWED" if r.review_decision else "PENDING_REVIEW",
                                   "review_decision": r.review_decision,
                                   "reviewer_id": r.reviewer_id, "review_reason": r.review_reason}
                                  for r in reinspections]}

    @app.get("/api/v1/work-orders/{order_id}")
    def get_order(order_id: str, user: UserRow = Depends(current_user),
                  db: Session = Depends(db_session)):
        row = order_for_tenant(db, order_id, user)
        return order_payload(db, row)

    @app.post("/api/v1/work-orders/{order_id}/start")
    def start_order(order_id: str, payload: StartOrderInput,
                    user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        require_role(user, {Role.ENGINEER}, db, "START_WORK_ORDER")
        row = order_for_tenant(db, order_id, user)
        if not payload.reason.strip():
            raise APIError(422, "REASON_REQUIRED", "开始处置必须填写原因")
        result = db.execute(update(WorkOrderRow).where(WorkOrderRow.id == order_id,
                    WorkOrderRow.status == "OPEN", WorkOrderRow.version == payload.expected_version)
                    .values(status="IN_PROGRESS", assignee_id=user.id,
                            version=payload.expected_version + 1))
        if result.rowcount != 1:
            db.rollback()
            raise APIError(409, "WORK_ORDER_VERSION_CONFLICT", "工单状态已变化")
        db.add(WorkOrderEventRow(id=str(uuid4()), work_order_id=order_id,
                                 event_type="STARTED", actor_id=user.id,
                                 payload={"reason": payload.reason.strip()}))
        db.commit()
        db.refresh(row)
        return order_payload(db, row)

    def work_order_aggregate(db: Session, row: WorkOrderRow) -> WorkOrder:
        records = db.scalars(select(ReinspectionRow).where(
            ReinspectionRow.work_order_id == row.id).order_by(ReinspectionRow.created_at,
                                                               ReinspectionRow.id)).all()
        return WorkOrder(id=row.id, status=WorkOrderStatus(row.status), version=row.version,
                         reinspections=[Reinspection(id=r.id, inspector_id=r.inspector_id,
                           image_key=r.image_key, result=ReinspectionResult(r.result), note=r.note,
                           decision=ReinspectionDecision(r.review_decision) if r.review_decision else None,
                           reviewer_id=r.reviewer_id, review_reason=r.review_reason) for r in records])

    @app.post("/api/v1/work-orders/{order_id}/handling")
    def complete_handling(order_id: str, payload: HandlingInput,
                          user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        require_role(user, {Role.ENGINEER}, db, "COMPLETE_HANDLING")
        row = order_for_tenant(db, order_id, user)
        if row.status not in {status.value for status in WorkOrderStatus}:
            raise APIError(409, "INVALID_WORK_ORDER_STATE", "工单尚未开始处置")
        aggregate = work_order_aggregate(db, row)
        try:
            record = aggregate.complete_handling(actor_id=user.id, actor_role=Role(user.role),
                                                 action=payload.action, expected_version=payload.expected_version)
        except PolicyError as exc:
            raise policy_error(exc) from None
        result = db.execute(update(WorkOrderRow).where(WorkOrderRow.id == order_id,
                    WorkOrderRow.status == "IN_PROGRESS", WorkOrderRow.version == payload.expected_version)
                    .values(status=aggregate.status.value, version=aggregate.version))
        if result.rowcount != 1:
            db.rollback()
            raise APIError(409, "WORK_ORDER_VERSION_CONFLICT", "工单状态已变化")
        db.add(WorkOrderEventRow(id=str(uuid4()), work_order_id=order_id,
                                 event_type="HANDLING_COMPLETED", actor_id=user.id,
                                 payload={"action": record.action}))
        db.commit()
        db.refresh(row)
        return order_payload(db, row)

    @app.post("/api/v1/work-orders/{order_id}/reinspections", status_code=201)
    async def submit_reinspection(order_id: str, image: UploadFile = File(...),
                                  result: ReinspectionResult = Form(...), note: str = Form(...),
                                  expected_version: int = Form(...),
                                  user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        require_role(user, {Role.INSPECTOR}, db, "SUBMIT_REINSPECTION")
        row = order_for_tenant(db, order_id, user)
        if row.status not in {status.value for status in WorkOrderStatus}:
            raise APIError(409, "INVALID_WORK_ORDER_STATE", "工单尚未进入复检")
        data = await image.read(MAX_IMAGE_BYTES + 1)
        inspect_image(data, image.content_type)
        image_hash = sha256(data).hexdigest()
        image_key = f"reinspections/{order_id}/{uuid4().hex}-{image_hash[:12]}"
        aggregate = work_order_aggregate(db, row)
        try:
            record = aggregate.submit_reinspection(inspector_id=user.id, image_key=image_key,
                result=result, note=note, expected_version=expected_version)
        except PolicyError as exc:
            raise policy_error(exc) from None
        updated = db.execute(update(WorkOrderRow).where(WorkOrderRow.id == order_id,
                    WorkOrderRow.status == "PENDING_REINSPECTION",
                    WorkOrderRow.version == expected_version).values(version=aggregate.version))
        if updated.rowcount != 1:
            db.rollback()
            raise APIError(409, "WORK_ORDER_VERSION_CONFLICT", "工单状态已变化")
        target = storage_root / image_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        db.add(ReinspectionRow(id=record.id, work_order_id=order_id,
                               order_version=aggregate.version, image_key=image_key,
                               result=record.result.value, inspector_id=user.id, note=record.note))
        db.add(WorkOrderEventRow(id=str(uuid4()), work_order_id=order_id,
                                 event_type="REINSPECTION_SUBMITTED", actor_id=user.id,
                                 payload={"reinspection_id": record.id, "result": record.result.value}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            target.unlink(missing_ok=True)
            raise APIError(409, "WORK_ORDER_VERSION_CONFLICT", "复检状态已变化") from None
        db.refresh(row)
        return order_payload(db, row)

    @app.post("/api/v1/work-orders/{order_id}/reinspections/{reinspection_id}/review")
    def review_reinspection(order_id: str, reinspection_id: str, payload: ReviewReinspectionInput,
                            user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        require_role(user, {Role.ENGINEER}, db, "REVIEW_REINSPECTION")
        row = order_for_tenant(db, order_id, user)
        if row.status not in {status.value for status in WorkOrderStatus}:
            raise APIError(409, "INVALID_WORK_ORDER_STATE", "工单尚未进入复检")
        aggregate = work_order_aggregate(db, row)
        if not aggregate.reinspections or aggregate.reinspections[-1].id != reinspection_id:
            raise APIError(409, "REINSPECTION_VERSION_CONFLICT", "只能确认当前待审复检")
        try:
            record = aggregate.review_reinspection(reviewer_id=user.id, reviewer_role=Role(user.role),
                decision=payload.decision, expected_version=payload.expected_version, reason=payload.reason)
        except PolicyError as exc:
            raise policy_error(exc) from None
        updated = db.execute(update(WorkOrderRow).where(WorkOrderRow.id == order_id,
                    WorkOrderRow.status == "PENDING_REINSPECTION",
                    WorkOrderRow.version == payload.expected_version)
                    .values(status=aggregate.status.value, version=aggregate.version))
        if updated.rowcount != 1:
            db.rollback()
            raise APIError(409, "WORK_ORDER_VERSION_CONFLICT", "工单状态已变化")
        target = db.get(ReinspectionRow, record.id)
        target.review_decision = record.decision.value
        target.reviewer_id = user.id
        target.review_reason = record.review_reason
        db.add(WorkOrderEventRow(id=str(uuid4()), work_order_id=order_id,
                                 event_type="REINSPECTION_REVIEWED", actor_id=user.id,
                                 payload={"reinspection_id": record.id,
                                          "decision": record.decision.value,
                                          "reason": record.review_reason}))
        db.commit()
        db.refresh(row)
        return order_payload(db, row)

    register_dsh_console_routes(app, storage_root=storage_root, db_session=db_session,
                                current_user=current_user, require_role=require_role, api_error=APIError)
    return app


app = create_app()
