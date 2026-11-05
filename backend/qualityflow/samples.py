"""Curated, tenant-isolated visual samples; no training happens on upload."""
from hashlib import sha256
import json
import re
from uuid import uuid4

from fastapi import Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import AuditRow, BatchRow, VisualSampleRow
from .detector import DetectorConfigError, detector_config
from .domain import Role


def register_sample_routes(app, *, db_session, current_user, require_role, inspect_image,
                           private_image, storage_root, api_error):
    def payload(row):
        return {"sample_id": row.id, "product_code": row.product_code, "label": row.label,
                "split": row.split, "source": row.source, "note": row.note,
                "image_sha256": row.image_sha256, "image_mime": row.image_mime,
                "image_width": row.image_width, "image_height": row.image_height,
                "created_at": row.created_at.isoformat(),
                "image_path": f"/api/v1/visual-samples/{row.id}/image"}

    def rows_for(db, user, product_code):
        return db.scalars(select(VisualSampleRow).where(VisualSampleRow.tenant_id == user.tenant_id,
            VisualSampleRow.product_code == product_code).order_by(VisualSampleRow.created_at, VisualSampleRow.id)).all()

    @app.get("/api/v1/detector")
    def capabilities(user=Depends(current_user), db: Session = Depends(db_session)):
        products = set(db.scalars(select(BatchRow.product_code).where(BatchRow.tenant_id == user.tenant_id).distinct()).all())
        products.update(db.scalars(select(VisualSampleRow.product_code).where(VisualSampleRow.tenant_id == user.tenant_id).distinct()).all())
        profiles = []
        for product in sorted(products):
            try:
                config = detector_config(product, app.state.detector_mode)
                profiles.append({"product_code": product, "configured": True,
                    **{k: v for k, v in config.items() if k != "endpoint"}})
            except DetectorConfigError:
                profiles.append({"product_code": product, "configured": False})
        return {"mode": app.state.detector_mode, "profiles": profiles,
                "model_validated": False, "sample_upload_trains_model": False}

    @app.get("/api/v1/visual-samples")
    def list_samples(product_code: str, limit: int = 100, user=Depends(current_user), db: Session = Depends(db_session)):
        if not 1 <= limit <= 100:
            raise api_error(422, "VALIDATION_ERROR", "limit 必须在 1 到 100 之间")
        rows = rows_for(db, user, product_code)
        counts = {split: {label: 0 for label in ("NORMAL", "ABNORMAL")} for split in ("TRAIN", "VALIDATION", "TEST")}
        for row in rows:
            counts[row.split][row.label] += 1
        return {"items": [payload(row) for row in rows[-limit:]], "total": len(rows), "counts": counts}

    @app.post("/api/v1/visual-samples", status_code=201)
    async def add_sample(product_code: str = Form(...), label: str = Form(...), split: str = Form(...),
                         source: str = Form(..., max_length=200), note: str = Form(default="", max_length=2000),
                         image: UploadFile = File(...), user=Depends(current_user), db: Session = Depends(db_session)):
        require_role(user, {Role.ENGINEER, Role.MANAGER, Role.ADMIN}, db, "ADD_VISUAL_SAMPLE")
        if label not in {"NORMAL", "ABNORMAL"} or split not in {"TRAIN", "VALIDATION", "TEST"} or not source.strip():
            raise api_error(422, "INVALID_SAMPLE", "请填写人工标签、数据划分和样本来源")
        if split == "TRAIN" and label != "NORMAL":
            raise api_error(422, "INVALID_SAMPLE_SPLIT", "当前异常检测基线只接受正常图像进入训练集")
        product_code = product_code.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", product_code):
            raise api_error(422, "INVALID_PRODUCT", "产品代码须为 1 至 64 位字母、数字、短横线或下划线")
        data = await image.read(20 * 1024 * 1024 + 1)
        mime, width, height = inspect_image(data, image.content_type)
        digest = sha256(data).hexdigest()
        source, note = source.strip(), note.strip()

        def replay(existing):
            if (existing.product_code, existing.label, existing.split, existing.source, existing.note) != (product_code, label, split, source, note):
                raise api_error(409, "SAMPLE_CONTENT_CONFLICT", "同一图像已有不同标签或划分，禁止重复进入训练和评测集")
            return {**payload(existing), "replayed": True}

        query = select(VisualSampleRow).where(VisualSampleRow.tenant_id == user.tenant_id, VisualSampleRow.image_sha256 == digest)
        existing = db.scalar(query)
        if existing:
            return replay(existing)
        identifier = f"VS-{uuid4().hex[:12]}"
        key = f"samples/{identifier}/image"
        row = VisualSampleRow(id=identifier, tenant_id=user.tenant_id, created_by=user.id,
            product_code=product_code, label=label, split=split, source=source, note=note,
            image_key=key, image_sha256=digest, image_mime=mime, image_width=width, image_height=height)
        db.add(row)
        db.add(AuditRow(id=str(uuid4()), tenant_id=user.tenant_id, actor_id=user.id,
            action="VISUAL_SAMPLE_ADDED", resource_type="visual_sample", resource_id=identifier,
            payload={"product_code": product_code, "label": label, "split": split, "image_sha256": digest}))
        target = storage_root / key
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            db.commit()
        except IntegrityError:
            db.rollback()
            target.unlink(missing_ok=True)
            existing = db.scalar(query)
            if existing:
                return replay(existing)
            raise
        except Exception:
            db.rollback()
            target.unlink(missing_ok=True)
            raise
        return {**payload(row), "replayed": False}

    @app.get("/api/v1/visual-samples/manifest")
    def manifest(product_code: str, user=Depends(current_user), db: Session = Depends(db_session)):
        items = [payload(row) for row in rows_for(db, user, product_code)]
        # The digest identifies the precise immutable selection for an external trainer.
        body = {"schema_version": 1, "product_code": product_code, "items": items}
        digest = sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {**body, "manifest_sha256": digest, "pixel_masks_available": False}

    @app.get("/api/v1/visual-samples/{sample_id}/image")
    def sample_image(sample_id: str, user=Depends(current_user), db: Session = Depends(db_session)):
        row = db.get(VisualSampleRow, sample_id)
        if row is None or row.tenant_id != user.tenant_id:
            raise api_error(404, "NOT_FOUND", "样本不存在")
        return private_image(row.image_key, row.image_mime)
