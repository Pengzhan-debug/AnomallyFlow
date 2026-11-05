from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from PIL import Image

from qualityflow.api import create_app
from qualityflow.db import ApprovalRow, InspectionRow, PipelineJobRow, ReportRow, UserRow
from qualityflow.seed import DEMO_USERS, seed_demo
from sqlalchemy import select
from qualityflow.worker import run_once


PASSWORDS = {name: f"long-demo-password-for-{name}" for name in DEMO_USERS}


def png_bytes(color: str = "white") -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), color).save(output, format="PNG")
    return output.getvalue()


class TestAPI:
    def setup_method(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.app = create_app(database_url=f"sqlite:///{root / 'test.db'}",
                              storage_root=root / "storage", create_schema=True, agent_mode="mock", detector_mode="mock")
        with self.app.state.session_factory() as db:
            seed_demo(db, tenant_id="demo-factory", passwords=PASSWORDS)
        self.client = TestClient(self.app)

    def upload(self, path, **kwargs):
        response = self.client.post(path, **kwargs)
        if response.status_code == 202:
            run_once(self.app.state.session_factory)
        return response

    def teardown_method(self):
        self.client.close()
        self.app.state.engine.dispose()
        self.temp.cleanup()

    def token(self, username: str) -> str:
        response = self.client.post("/api/v1/auth/login", json={
            "username": username, "password": PASSWORDS[username]})
        assert response.status_code == 200, response.json()
        return response.json()["access_token"]

    @staticmethod
    def auth(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def test_normal_upload_is_validated_persisted_and_idempotent(self):
        token = self.token("inspector.demo")
        image = png_bytes()
        headers = {**self.auth(token), "Idempotency-Key": "normal-case-1"}
        payload = {"batch_id": "BA-2026-1001", "product_code": "VALVE-A"}
        first = self.upload("/api/v1/inspections", headers=headers, data=payload,
                                 files={"image": ("part.png", image, "image/png")})
        assert first.status_code == 202, first.json()
        assert first.json()["status"] == "PENDING"
        task_id = first.json()["task_id"]
        detail = self.client.get(f"/api/v1/inspections/{task_id}", headers=self.auth(token))
        assert detail.status_code == 200
        assert detail.json()["status"] == "COMPLETED"
        assert detail.json()["detection_status"] == "NORMAL"
        repeated = self.upload("/api/v1/inspections", headers=headers, data=payload,
                                    files={"image": ("other-name.png", image, "image/png")})
        assert repeated.status_code == 202
        assert repeated.json()["task_id"] == task_id
        assert repeated.json()["replayed"] is True
        changed = self.upload("/api/v1/inspections", headers=headers, data=payload,
                                   files={"image": ("part.png", png_bytes("black"), "image/png")})
        assert changed.status_code == 409
        assert changed.json()["error_code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_upload_and_queue_survive_api_restart_before_worker_runs(self):
        auth = {**self.auth(self.token("inspector.demo")), "Idempotency-Key": "queue-restart-test"}
        args = {"headers": auth, "data": {"batch_id": "BA-2026-1003", "product_code": "VALVE-A"},
                "files": {"image": ("part.png", png_bytes(), "image/png")}}
        first = self.client.post("/api/v1/inspections", **args)
        task_id = first.json()["task_id"]
        assert first.status_code == 202
        assert self.client.get(f"/api/v1/inspections/{task_id}", headers=auth).json()["detection_status"] is None
        replay = self.client.post("/api/v1/inspections", **args)
        assert replay.json()["task_id"] == task_id
        with self.app.state.session_factory() as db:
            assert len(db.scalars(select(PipelineJobRow)).all()) == 1
        restarted = create_app(database_url=str(self.app.state.engine.url))
        try:
            assert run_once(restarted.state.session_factory)
            after = self.client.get(f"/api/v1/inspections/{task_id}", headers=auth).json()
            assert after["status"] == "WAITING_APPROVAL"
            assert after["execution"]["status"] == "SUCCEEDED"
        finally:
            restarted.state.engine.dispose()

    def test_abnormal_and_error_mock_paths_are_not_claimed_complete(self):
        token = self.token("inspector.demo")
        for batch, expected, status in [("BA-2026-1003", "ABNORMAL", "WAITING_APPROVAL"),
                                        ("BA-2026-1004", "UNCERTAIN", "WAITING_APPROVAL"),
                                        ("BA-2026-1005", "ERROR", "NEEDS_REVIEW")]:
            response = self.upload("/api/v1/inspections",
                headers={**self.auth(token), "Idempotency-Key": f"case-{batch}"},
                data={"batch_id": batch, "product_code": "VALVE-A"},
                files={"image": ("part.png", png_bytes(), "image/png")})
            assert response.status_code == 202, response.json()
            task = self.client.get(f"/api/v1/inspections/{response.json()['task_id']}",
                                   headers=self.auth(token)).json()
            assert task["detection_status"] == expected
            assert task["status"] == status
            if expected == "ERROR":
                assert task["error_code"] == "MOCK_DETECTOR_ERROR"
                assert task["report_id"] is None
            else:
                assert task["error_code"] is None
                assert task["report_id"] and task["approval_id"]

    def test_generated_report_references_real_sources_and_enters_approval(self):
        inspector = self.auth(self.token("inspector.demo"))
        engineer = self.auth(self.token("engineer.demo"))
        manager = self.auth(self.token("manager.demo"))
        generated = {}
        for batch, risk in [("BA-2026-1002", "MEDIUM"),
                            ("BA-2026-1003", "HIGH"),
                            ("BA-2026-1006", "MEDIUM")]:
            upload = self.upload("/api/v1/inspections",
                headers={**inspector, "Idempotency-Key": f"report-{batch}"},
                data={"batch_id": batch, "product_code": "VALVE-A"},
                files={"image": ("part.png", png_bytes(), "image/png")})
            assert upload.status_code == 202, upload.json()
            task_id = upload.json()["task_id"]
            detail = self.client.get(f"/api/v1/inspections/{task_id}",
                                     headers=inspector).json()
            assert detail["status"] == "WAITING_APPROVAL"
            generated[batch] = detail
            report = self.client.get(f"/api/v1/inspections/{task_id}/report",
                                     headers=inspector)
            assert report.status_code == 200, report.json()
            body = report.json()
            assert body["risk_level"] == risk
            assert body["evidence_sufficient"] is False
            assert len(body["evidence_refs"]) == (2 if batch == "BA-2026-1006" else 4)
            assert len(body["tool_trace"]) == 4
            assert {item["tool_name"] for item in body["tool_trace"]} == {
                "get_batch_info", "get_inspection_history", "search_quality_cases", "search_sop_documents"}
            assert "根因尚未确认" in body["defect_summary"]
            for item in body["evidence_refs"]:
                source = self.client.get(
                    f"/api/v1/inspections/{task_id}/evidence/{item['id']}",
                    headers=engineer)
                assert source.status_code == 200
                assert source.json()["source_id"] == item["source_id"]
            if batch == "BA-2026-1006":
                assert "历史质量案例检索无结果" in body["missing_evidence"]
        medium = generated["BA-2026-1002"]
        approved = self.client.post(f"/api/v1/approvals/{medium['approval_id']}/decision",
            headers=engineer, json={"decision": "APPROVE", "reason": "复核后启动处置",
                                    "expected_version": 0, "expected_report_version": 1})
        assert approved.status_code == 200, approved.json()
        assert approved.json()["work_order_id"]
        assert self.client.get(f"/api/v1/inspections/{medium['task_id']}",
                               headers=inspector).json()["status"] == "ORDER_CREATED"
        high = generated["BA-2026-1003"]
        engineer_step = self.client.post(f"/api/v1/approvals/{high['approval_id']}/decision",
            headers=engineer, json={"decision": "APPROVE", "reason": "提交共同审批",
                                    "expected_version": 0, "expected_report_version": 1})
        assert engineer_step.status_code == 200, engineer_step.json()
        assert engineer_step.json()["work_order_id"] is None
        manager_step = self.client.post(f"/api/v1/approvals/{high['approval_id']}/decision",
            headers=manager, json={"decision": "APPROVE", "reason": "独立复核通过",
                                   "expected_version": 1, "expected_report_version": 1})
        assert manager_step.status_code == 200, manager_step.json()
        assert manager_step.json()["work_order_id"]
        invalid = self.upload("/api/v1/inspections",
            headers={**inspector, "Idempotency-Key": "invalid-report-12"},
            data={"batch_id": "BA-2026-1012", "product_code": "VALVE-A"},
            files={"image": ("part.png", png_bytes(), "image/png")})
        assert invalid.status_code == 202
        task_id = invalid.json()["task_id"]
        detail = self.client.get(f"/api/v1/inspections/{task_id}", headers=inspector).json()
        assert detail["status"] == "NEEDS_REVIEW"
        assert detail["error_code"] == "EVIDENCE_VALIDATION_FAILED"
        assert detail["report_id"] is None and detail["approval_id"] is None
        assert self.client.get(f"/api/v1/inspections/{task_id}/report",
                               headers=inspector).status_code == 404

    def test_read_tool_api_is_scoped_and_audited(self):
        self.make_approval()
        engineer = self.auth(self.token("engineer.demo"))
        base = "/api/v1/inspections/QI-workflow-HIGH/tools"
        found = self.client.post(base, headers=engineer, json={"tool_name": "search_sop_documents",
                                 "arguments": {"query": "阀体表面异常 反光"}})
        assert found.status_code == 200, found.json()
        assert found.json()["status"] == "OK"
        assert len(found.json()["evidence"]) == 1
        denied = self.client.post(base, headers=engineer, json={"tool_name": "get_batch_info",
                                  "arguments": {"batch_id": "other-tenant-batch"}})
        assert denied.json()["error_code"] == "FORBIDDEN"
        bad = self.client.post(base, headers=engineer, json={"tool_name": "search_quality_cases",
                               "arguments": {"tenant_id": "other", "query": "阀体"}})
        assert bad.json()["error_code"] == "INVALID_TOOL_ARGUMENTS"
        trace = self.client.get(base, headers=engineer).json()["items"]
        assert [item["status"] for item in trace] == ["OK", "ERROR", "ERROR"]
        write_tool = self.client.post(base, headers=engineer, json={"tool_name": "create_work_order"})
        assert write_tool.status_code == 422

    def test_upload_rejects_invalid_content_batch_and_role(self):
        inspector = self.token("inspector.demo")
        engineer = self.token("engineer.demo")
        data = {"batch_id": "BA-2026-1001", "product_code": "VALVE-A"}
        invalid = self.upload("/api/v1/inspections",
            headers={**self.auth(inspector), "Idempotency-Key": "invalid-image-1"},
            data=data, files={"image": ("fake.png", b"not an image", "image/png")})
        assert invalid.status_code == 422
        assert invalid.json()["error_code"] == "INVALID_IMAGE"
        bad_batch = self.upload("/api/v1/inspections",
            headers={**self.auth(inspector), "Idempotency-Key": "bad-batch-1"},
            data={"batch_id": "BA-2026-1001", "product_code": "OTHER"},
            files={"image": ("part.png", png_bytes(), "image/png")})
        assert bad_batch.status_code == 422
        forbidden = self.upload("/api/v1/inspections",
            headers={**self.auth(engineer), "Idempotency-Key": "forbidden-1"},
            data=data, files={"image": ("part.png", png_bytes(), "image/png")})
        assert forbidden.status_code == 403
        assert forbidden.json()["error_code"] == "FORBIDDEN"
        unauthenticated = self.client.get("/api/v1/batches")
        assert unauthenticated.status_code == 401

    def test_admin_creation_requires_password_change_and_is_not_self_service(self):
        inspector = self.token("inspector.demo")
        payload = {"username": "new.engineer", "display_name": "新工程师",
                   "role": "ENGINEER", "reason": "新增岗位"}
        denied = self.client.post("/api/v1/users", headers=self.auth(inspector), json=payload)
        assert denied.status_code == 403
        admin = self.token("admin.demo")
        listed = self.client.get("/api/v1/users", headers=self.auth(admin))
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 5
        assert "password_hash" not in listed.text
        created = self.client.post("/api/v1/users", headers=self.auth(admin), json=payload)
        assert created.status_code == 201, created.json()
        temporary = created.json()["temporary_password"]
        duplicate = self.client.post("/api/v1/users", headers=self.auth(admin), json=payload)
        assert duplicate.status_code == 409
        login = self.client.post("/api/v1/auth/login", json={
            "username": "new.engineer", "password": temporary})
        assert login.status_code == 200
        assert login.json()["must_change_password"] is True
        new_token = login.json()["access_token"]
        locked = self.client.get("/api/v1/batches", headers=self.auth(new_token))
        assert locked.status_code == 403
        assert locked.json()["error_code"] == "PASSWORD_CHANGE_REQUIRED"
        changed = self.client.post("/api/v1/auth/change-password",
            headers=self.auth(new_token), json={"old_password": temporary,
                                                "new_password": "a-new-long-password-123"})
        assert changed.status_code == 200, changed.json()
        assert self.client.get("/api/v1/batches", headers=self.auth(new_token)).status_code == 200
        disabled = self.client.post("/api/v1/users", headers=self.auth(admin), json={
            "username": "disabled.demo", "display_name": "待启用用户", "role": "INSPECTOR",
            "active": False, "reason": "预建账户"})
        assert disabled.status_code == 201, disabled.json()
        assert disabled.json()["active"] is False
        denied_login = self.client.post("/api/v1/auth/login", json={
            "username": "disabled.demo", "password": disabled.json()["temporary_password"]})
        assert denied_login.status_code == 401
        self_lockout = self.client.patch("/api/v1/users/" + self._user_id("admin.demo"),
            headers=self.auth(admin), json={"role": "INSPECTOR", "active": False,
                                            "reason": "误操作"})
        assert self_lockout.status_code == 409

    def _user_id(self, username: str) -> str:
        with self.app.state.session_factory() as db:
            return db.scalar(select(UserRow.id).where(UserRow.username == username))

    def make_approval(self, risk: str = "HIGH") -> str:
        with self.app.state.session_factory() as db:
            inspector = db.scalar(select(UserRow).where(UserRow.username == "inspector.demo"))
            task = InspectionRow(id=f"QI-workflow-{risk}", tenant_id="demo-factory",
                created_by=inspector.id, batch_id="BA-2026-1003", product_code="VALVE-A",
                image_key="test/original.png", image_sha256="a" * 64, image_mime="image/png",
                image_width=8, image_height=8, remark="", status="WAITING_APPROVAL",
                idempotency_key=f"workflow-{risk}", request_hash="b" * 64)
            report = ReportRow(id=f"report-{risk}", task_id=task.id, version=1,
                               report_json={"summary": "synthetic"}, evidence_sufficient=True)
            approval = ApprovalRow(id=f"approval-{risk}", report_id=report.id,
                task_id=task.id, report_version=1, risk_level=risk,
                stage="ENGINEER", status="PENDING", version=0, decisions_json=[])
            db.add(task)
            db.flush()
            db.add(report)
            db.flush()
            db.add(approval)
            db.commit()
            return approval.id

    def test_high_risk_cosign_and_uncertain_reinspection_rounds(self):
        approval_id = self.make_approval()
        engineer = self.auth(self.token("engineer.demo"))
        manager = self.auth(self.token("manager.demo"))
        inspector = self.auth(self.token("inspector.demo"))
        endpoint = f"/api/v1/approvals/{approval_id}/decision"
        first = self.client.post(endpoint, headers=engineer, json={
            "decision": "APPROVE", "reason": "已核对证据，提交主管共同确认",
            "expected_version": 0, "expected_report_version": 1})
        assert first.status_code == 200, first.json()
        assert first.json()["stage"] == "MANAGER"
        assert first.json()["status"] == "PENDING"
        assert first.json()["work_order_id"] is None
        wrong = self.client.post(endpoint, headers=engineer, json={
            "decision": "REJECT", "reason": "反向决定", "expected_version": 1,
            "expected_report_version": 1})
        assert wrong.status_code == 409
        second = self.client.post(endpoint, headers=manager, json={
            "decision": "APPROVE", "reason": "主管独立复核通过",
            "expected_version": 1, "expected_report_version": 1})
        assert second.status_code == 200, second.json()
        assert second.json()["status"] == "APPROVED"
        order_id = second.json()["work_order_id"]
        assert order_id
        replay = self.client.post(endpoint, headers=manager, json={
            "decision": "APPROVE", "reason": "主管独立复核通过",
            "expected_version": 1, "expected_report_version": 1})
        assert replay.status_code == 200
        assert replay.json()["work_order_id"] == order_id
        order_base = f"/api/v1/work-orders/{order_id}"
        start = self.client.post(order_base + "/start", headers=engineer,
            json={"reason": "开始现场处置", "expected_version": 0})
        assert start.status_code == 200, start.json()
        handling = self.client.post(order_base + "/handling", headers=engineer,
            json={"action": "清洁并复核设备参数", "expected_version": 1})
        assert handling.status_code == 200, handling.json()
        uncertain = self.client.post(order_base + "/reinspections", headers=inspector,
            data={"result": "UNCERTAIN", "note": "反光导致无法判断", "expected_version": 2},
            files={"image": ("round1.png", png_bytes(), "image/png")})
        assert uncertain.status_code == 201, uncertain.json()
        assert uncertain.json()["reinspections"][-1]["order_version"] == 3
        ri_id = uncertain.json()["reinspections"][-1]["id"]
        close = self.client.post(order_base + f"/reinspections/{ri_id}/review", headers=engineer,
            json={"decision": "CONFIRM_PASS", "reason": "尝试关闭", "expected_version": 3})
        assert close.status_code == 409
        recapture = self.client.post(order_base + f"/reinspections/{ri_id}/review", headers=engineer,
            json={"decision": "REQUEST_RECAPTURE", "reason": "重新取样并避开反光", "expected_version": 3})
        assert recapture.status_code == 200, recapture.json()
        assert recapture.json()["status"] == "PENDING_REINSPECTION"
        second_round = self.client.post(order_base + "/reinspections", headers=inspector,
            data={"result": "PASS", "note": "新图像未见异常", "expected_version": 4},
            files={"image": ("round2.png", png_bytes("white"), "image/png")})
        assert second_round.status_code == 201, second_round.json()
        assert len(second_round.json()["reinspections"]) == 2
        assert second_round.json()["reinspections"][-1]["order_version"] == 5
        ri2_id = second_round.json()["reinspections"][-1]["id"]
        confirmed = self.client.post(order_base + f"/reinspections/{ri2_id}/review", headers=engineer,
            json={"decision": "CONFIRM_PASS", "reason": "新图像清晰且复核通过", "expected_version": 5})
        assert confirmed.status_code == 200, confirmed.json()
        assert confirmed.json()["status"] == "CLOSED"

    def test_high_risk_requires_reason_and_current_report(self):
        approval_id = self.make_approval()
        engineer = self.auth(self.token("engineer.demo"))
        endpoint = f"/api/v1/approvals/{approval_id}/decision"
        no_reason = self.client.post(endpoint, headers=engineer, json={
            "decision": "APPROVE", "expected_version": 0, "expected_report_version": 1})
        assert no_reason.status_code == 422
        with self.app.state.session_factory() as db:
            db.add(ReportRow(id="new-report", task_id="QI-workflow-HIGH", version=2,
                             report_json={}, evidence_sufficient=True))
            db.commit()
        stale = self.client.post(endpoint, headers=engineer, json={
            "decision": "APPROVE", "reason": "旧报告", "expected_version": 0,
            "expected_report_version": 1})
        assert stale.status_code == 409
        assert stale.json()["error_code"] == "REPORT_VERSION_CONFLICT"
