"""Explicit demo-only process termination probe. Never runs during normal API/worker startup."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

import httpx
from sqlalchemy import select, update

from .db import ApprovalRow, PipelineJobRow, ReportRow, WorkOrderRow, utc_now
from .pipeline import execute_pipeline
from .worker import claim_job, run_claim, run_once, session_factory_for


def crash_and_recover(factory, task_id: str, *, database_url: str | None = None):
    env = dict(os.environ, DEMO_MODE="1")
    if database_url:
        env["DATABASE_URL"] = database_url
    with TemporaryDirectory() as directory:
        marker = Path(directory) / "prepared"
        process = subprocess.Popen([sys.executable, "-m", "qualityflow.recovery_check",
            "--child", task_id, str(marker)], env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 30
            while not marker.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("Crash probe did not reach uncommitted results")
                time.sleep(0.05)
            # Terminate the actual process after it flushed report/approval rows, before COMMIT.
            process.kill()
            process.wait(timeout=10)
            with factory() as db:
                assert not db.scalars(select(ReportRow).where(ReportRow.task_id == task_id)).all()
            deadline = time.monotonic() + 10
            claim = None
            while claim is None and time.monotonic() < deadline:
                claim = claim_job(factory, task_id=task_id)
                if claim is None:
                    time.sleep(0.1)
            assert claim is not None, "Expired job was not reclaimed"
            assert run_claim(factory, claim), "Replacement worker did not finish"
            with factory() as db:
                assert len(db.scalars(select(ReportRow).where(ReportRow.task_id == task_id)).all()) == 1
                assert len(db.scalars(select(ApprovalRow).where(ApprovalRow.task_id == task_id)).all()) == 1
                job = db.scalar(select(PipelineJobRow).where(PipelineJobRow.task_id == task_id))
                assert job.status == "SUCCEEDED" and job.attempts == 2
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def main():
    if os.environ.get("DEMO_MODE") != "1":
        raise SystemExit("This failure injection check requires DEMO_MODE=1")
    factory = session_factory_for()
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--child":
            task_id, marker = sys.argv[2:4]
            claim = claim_job(factory, task_id=task_id, lease_seconds=3)
            assert claim is not None
            with factory() as db:
                execute_pipeline(db, task_id)
                db.flush()
                Path(marker).write_text("uncommitted", encoding="utf-8")
                time.sleep(60)
            return
        from .smoke_support import upload_demo_task
        with httpx.Client(base_url=os.environ.get("QUALITYFLOW_URL", "http://localhost:8000"), timeout=15) as client:
            task_id = upload_demo_task(client, "recovery-" + uuid4().hex)
            with factory() as db:
                db.execute(update(PipelineJobRow).where(PipelineJobRow.task_id == task_id).values(mode="mock"))
                db.commit()
            crash_and_recover(factory, task_id)
            def login(role):
                response = client.post("/api/v1/auth/login", json={"username": role.lower() + ".demo",
                    "password": os.environ[f"DEMO_{role}_PASSWORD"]})
                response.raise_for_status()
                return {"Authorization": "Bearer " + response.json()["access_token"]}
            engineer, manager = login("ENGINEER"), login("MANAGER")
            detail = client.get(f"/api/v1/inspections/{task_id}", headers=engineer)
            detail.raise_for_status()
            approval_id = detail.json()["approval_id"]
            for headers, version in [(engineer, 0), (manager, 1), (manager, 1)]:
                result = client.post(f"/api/v1/approvals/{approval_id}/decision", headers=headers,
                    json={"decision": "APPROVE", "reason": "合成恢复测试审批",
                          "expected_version": version, "expected_report_version": 1})
                result.raise_for_status()
            order_id = result.json()["work_order_id"]
            # Replay after a committed approval/order to verify the task's idempotent boundary.
            with factory() as db:
                db.execute(update(PipelineJobRow).where(PipelineJobRow.task_id == task_id)
                    .values(status="READY", attempts=0, available_at=utc_now()))
                db.commit()
            assert run_once(factory, task_id=task_id)
            with factory() as db:
                assert len(db.scalars(select(ReportRow).where(ReportRow.task_id == task_id)).all()) == 1
                orders = db.scalars(select(WorkOrderRow).where(WorkOrderRow.task_id == task_id)).all()
                assert len(orders) == 1 and orders[0].id == order_id
            print("Recovery check passed: killed process before commit, rollback, lease reclaim, one report/approval/order after replay.")
    finally:
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    main()
