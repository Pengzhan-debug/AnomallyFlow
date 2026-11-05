from datetime import timedelta
from hashlib import sha256

import pytest
from sqlalchemy import select

from test_api import TestAPI as Harness, png_bytes
from qualityflow.db import AuditRow, SessionRow, UserRow, VisualSampleRow, utc_now


@pytest.fixture
def app():
    value = Harness()
    value.setup_method()
    yield value
    value.teardown_method()


def add(app, auth, color="white", **overrides):
    return app.client.post("/api/v1/visual-samples", headers=auth,
        data={"product_code": "VALVE-A", "label": "NORMAL", "split": "TRAIN", "source": "合成测试图像", **overrides},
        files={"image": ("part.png", png_bytes(color), "image/png")})


def test_samples_deduplicate_prevent_split_leakage_and_export_reproducible_manifest(app):
    auth = app.auth(app.token("engineer.demo"))
    result = add(app, auth)
    assert result.status_code == 201, result.json()
    identifier = result.json()["sample_id"]
    assert add(app, auth).json()["replayed"] is True
    assert add(app, auth, split="TEST").status_code == 409
    assert add(app, auth, label="ABNORMAL", split="TEST").status_code == 409
    second = add(app, auth, "red", label="ABNORMAL", split="VALIDATION")
    assert second.status_code == 201
    listing = app.client.get("/api/v1/visual-samples?product_code=VALVE-A", headers=auth).json()
    assert listing["total"] == 2 and listing["counts"]["TRAIN"]["NORMAL"] == 1
    assert listing["counts"]["VALIDATION"]["ABNORMAL"] == 1
    manifest = app.client.get("/api/v1/visual-samples/manifest?product_code=VALVE-A", headers=auth).json()
    repeated = app.client.get("/api/v1/visual-samples/manifest?product_code=VALVE-A", headers=auth).json()
    assert manifest == repeated and len(manifest["manifest_sha256"]) == 64
    assert manifest["pixel_masks_available"] is False
    assert all("image_key" not in item and "tenant_id" not in item for item in manifest["items"])
    image = app.client.get(f"/api/v1/visual-samples/{identifier}/image", headers=auth)
    assert image.content == png_bytes() and image.headers["cache-control"] == "private, no-store"
    with app.app.state.session_factory() as db:
        assert len(db.scalars(select(VisualSampleRow)).all()) == 2
        assert len(db.scalars(select(AuditRow).where(AuditRow.action == "VISUAL_SAMPLE_ADDED")).all()) == 2


def test_sample_permissions_validation_and_tenant_isolation(app):
    engineer = app.auth(app.token("engineer.demo"))
    for role in ("inspector.demo", "auditor.demo"):
        assert add(app, app.auth(app.token(role))).status_code == 403
    for payload in ({"split": "TRAIN", "label": "ABNORMAL"}, {"label": "UNKNOWN"},
                    {"product_code": "bad/product"}, {"source": "  "}):
        assert add(app, engineer, **payload).status_code == 422
    invalid = app.client.post("/api/v1/visual-samples", headers=engineer,
        data={"product_code": "VALVE-A", "label": "NORMAL", "split": "TRAIN", "source": "test"},
        files={"image": ("fake.png", b"fake", "image/png")})
    assert invalid.status_code == 422
    sample_id = add(app, engineer).json()["sample_id"]
    with app.app.state.session_factory() as db:
        db.add(UserRow(id="foreign", tenant_id="other", username="foreign", display_name="other",
            role="ENGINEER", password_hash="unused", must_change_password=False))
        db.flush()
        db.add(SessionRow(token_hash=sha256(b"foreign-token").hexdigest(), user_id="foreign", expires_at=utc_now() + timedelta(hours=1)))
        db.commit()
    foreign = app.auth("foreign-token")
    assert app.client.get(f"/api/v1/visual-samples/{sample_id}/image", headers=foreign).status_code == 404
    assert app.client.get("/api/v1/visual-samples?product_code=VALVE-A", headers=foreign).json()["total"] == 0
    assert app.client.get("/api/v1/visual-samples/manifest?product_code=VALVE-A", headers=foreign).json()["items"] == []


def test_sample_category_can_be_prepared_before_production_batches_exist(app):
    auth = app.auth(app.token("engineer.demo"))
    result = add(app, auth, product_code="PART-NEW")
    assert result.status_code == 201
    assert result.json()["product_code"] == "PART-NEW"
    profiles = app.client.get("/api/v1/detector", headers=auth).json()["profiles"]
    assert any(item["product_code"] == "PART-NEW" for item in profiles)
    batches = app.client.get("/api/v1/batches?product_code=PART-NEW", headers=auth).json()["items"]
    assert batches == []  # Curation never fabricates a production batch.
