from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select, update

from qualityflow.api import create_app
from qualityflow.db import (ApprovalRow, BatchRow, InspectionRow, PipelineJobRow,
                            ReportRow, UserRow, utc_now)
from qualityflow.recovery_check import crash_and_recover
from qualityflow.worker import claim_job, run_claim, run_once
import qualityflow.worker as worker


@pytest.fixture
def queue(tmp_path):
    url = f"sqlite:///{tmp_path / 'queue.db'}"
    app = create_app(database_url=url, create_schema=True)
    factory = app.state.session_factory
    with factory() as db:
        db.add(UserRow(id="user", tenant_id="tenant", username="user", display_name="测试",
                       role="INSPECTOR", password_hash="unused"))
        db.add(BatchRow(id="batch", tenant_id="tenant", product_code="VALVE-A", equipment_id="eq",
                        start_at=utc_now(), end_at=utc_now(), scenario_id="SC-03"))
        db.flush()
        db.add(InspectionRow(id="task", tenant_id="tenant", created_by="user", batch_id="batch",
            product_code="VALVE-A", image_key="unused", image_sha256="a" * 64, image_mime="image/png",
            image_width=8, image_height=8, status="PENDING", idempotency_key="test-key", request_hash="b" * 64))
        db.flush()
        db.add(PipelineJobRow(id="job", task_id="task", status="READY", attempts=0))
        db.commit()
    yield factory, url
    app.state.engine.dispose()


def expire(factory):
    with factory() as db:
        db.execute(update(PipelineJobRow).values(lease_until=utc_now() - timedelta(seconds=1),
                                                available_at=utc_now() - timedelta(seconds=1)))
        db.commit()


def test_two_workers_claim_once_and_replay_has_no_duplicate_results(queue):
    factory, _ = queue
    barrier = Barrier(2)
    def claim():
        barrier.wait()
        return claim_job(factory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))
    winners = [value for value in claims if value is not None]
    assert len(winners) == 1
    assert run_claim(factory, winners[0])
    assert not run_claim(factory, winners[0])
    assert not run_once(factory)
    with factory() as db:
        assert len(db.scalars(select(ReportRow)).all()) == 1
        assert len(db.scalars(select(ApprovalRow)).all()) == 1


def test_expired_token_cannot_submit_after_another_worker_reclaims(queue):
    factory, _ = queue
    old = claim_job(factory)
    expire(factory)
    replacement = claim_job(factory)
    assert replacement and replacement.token != old.token
    assert not run_claim(factory, old)
    assert run_claim(factory, replacement)
    with factory() as db:
        assert db.get(PipelineJobRow, "job").attempts == 2
        assert len(db.scalars(select(ReportRow)).all()) == 1


def test_execution_errors_retry_then_become_manual_review(queue, monkeypatch):
    factory, _ = queue
    def broken(*_):
        raise RuntimeError("synthetic pipeline fault")
    monkeypatch.setattr(worker, "execute_pipeline", broken)
    for _ in range(3):
        claim = claim_job(factory)
        assert claim
        assert not run_claim(factory, claim)
        expire(factory)
    with factory() as db:
        job = db.get(PipelineJobRow, "job")
        task = db.get(InspectionRow, "task")
        assert job.status == "FAILED" and job.attempts == 3
        assert task.status == "NEEDS_REVIEW" and task.error_code == "PIPELINE_RETRY_EXHAUSTED"
        assert not db.scalars(select(ReportRow)).all()
    assert not run_once(factory)


def test_repeated_crashes_exhaust_attempt_budget(queue):
    factory, _ = queue
    for _ in range(3):
        assert claim_job(factory)
        expire(factory)
    assert claim_job(factory) is None
    with factory() as db:
        assert db.get(PipelineJobRow, "job").status == "FAILED"
        assert db.get(InspectionRow, "task").error_code == "WORKER_LEASE_EXHAUSTED"


def test_lease_expiry_before_commit_rolls_back_partial_results(queue, monkeypatch):
    factory, _ = queue
    original = worker.execute_pipeline
    def expires_while_working(db, task_id):
        original(db, task_id)
        db.execute(update(PipelineJobRow).values(lease_until=utc_now() - timedelta(seconds=1)))
    monkeypatch.setattr(worker, "execute_pipeline", expires_while_working)
    assert not run_claim(factory, claim_job(factory))
    with factory() as db:
        assert not db.scalars(select(ReportRow)).all()
        assert db.get(InspectionRow, "task").status == "PENDING"
    monkeypatch.setattr(worker, "execute_pipeline", original)
    expire(factory)
    assert run_once(factory)


def test_real_process_kill_rolls_back_and_replacement_finishes_once(queue):
    factory, url = queue
    crash_and_recover(factory, "task", database_url=url)
