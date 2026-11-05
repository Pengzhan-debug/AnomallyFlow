"""Durable PostgreSQL/SQLite job worker with leases and fenced atomic results."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import signal
from threading import Event
from uuid import uuid4

from sqlalchemy import and_, create_engine, event, or_, select, update
from sqlalchemy.orm import sessionmaker

from .config import database_url_from_env
from .db import AuditRow, InspectionRow, PipelineJobRow, utc_now
from .pipeline import execute_pipeline

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Claim:
    job_id: str
    token: str
    task_id: str


class LeaseLost(Exception):
    pass


def session_factory_for(url=None):
    url = url or database_url_from_env()
    engine = create_engine(url, pool_pre_ping=True,
                           connect_args={"check_same_thread": False} if str(url).startswith("sqlite:") else {})
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    return sessionmaker(engine, expire_on_commit=False)


def eligible(now):
    return or_(and_(PipelineJobRow.status == "READY", PipelineJobRow.available_at <= now),
               and_(PipelineJobRow.status == "RUNNING", PipelineJobRow.lease_until <= now))


def mark_terminal(db, task_id: str, code: str):
    task = db.get(InspectionRow, task_id)
    if task is not None and task.status in {"PENDING", "INVESTIGATING"}:
        task.status = "NEEDS_REVIEW"
        task.error_code = code
        task.version += 1
        db.add(AuditRow(id=str(uuid4()), tenant_id=task.tenant_id, actor_id=None,
                        action="PIPELINE_FAILED", resource_type="inspection", resource_id=task.id,
                        payload={"error_code": code}))


def claim_job(factory, *, lease_seconds: float = 180, task_id: str | None = None) -> Claim | None:
    with factory() as db:
        now = utc_now()
        query = select(PipelineJobRow).where(eligible(now))
        if task_id is not None:
            query = query.where(PipelineJobRow.task_id == task_id)
        row = db.scalar(query
                        .order_by(PipelineJobRow.available_at, PipelineJobRow.id)
                        .with_for_update(skip_locked=True).limit(1))
        if row is None:
            return None
        token = str(uuid4())
        attempts = row.attempts
        exhausted = attempts >= MAX_ATTEMPTS
        result = db.execute(update(PipelineJobRow).where(
            PipelineJobRow.id == row.id, PipelineJobRow.attempts == attempts, eligible(now))
            .values(status="FAILED" if exhausted else "RUNNING", attempts=attempts if exhausted else attempts + 1,
                    lease_token=None if exhausted else token,
                    lease_until=None if exhausted else now + timedelta(seconds=lease_seconds),
                    finished_at=now if exhausted else None,
                    last_error_code="WORKER_LEASE_EXHAUSTED" if exhausted else row.last_error_code)
            .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            db.rollback()
            return None
        if exhausted:
            mark_terminal(db, row.task_id, "WORKER_LEASE_EXHAUSTED")
        db.commit()
        return None if exhausted else Claim(row.id, token, row.task_id)


def run_claim(factory, claim: Claim) -> bool:
    try:
        with factory() as db:
            job = db.scalar(select(PipelineJobRow).where(
                PipelineJobRow.id == claim.job_id, PipelineJobRow.status == "RUNNING",
                PipelineJobRow.lease_token == claim.token).with_for_update())
            if job is None:
                raise LeaseLost()
            until = job.lease_until
            if until is None or (until.replace(tzinfo=timezone.utc) if until.tzinfo is None else until) <= utc_now():
                raise LeaseLost()
            outcome = execute_pipeline(db, job.task_id) or "SUCCEEDED"
            updated = db.execute(update(PipelineJobRow).where(
                PipelineJobRow.id == claim.job_id, PipelineJobRow.lease_token == claim.token,
                PipelineJobRow.status == "RUNNING", PipelineJobRow.lease_until > utc_now())
                .values(status=outcome, lease_token=None, lease_until=None,
                        attempts=0 if outcome in {"READY", "WAITING_APPROVAL"} else job.attempts,
                        available_at=utc_now(), last_error_code=None,
                        finished_at=utc_now() if outcome == "SUCCEEDED" else None)
                .execution_options(synchronize_session=False))
            if updated.rowcount != 1:
                raise LeaseLost()
            # Report, evidence, approval, audit and job acknowledgement share this transaction.
            db.commit()
        return True
    except LeaseLost:
        logger.info("Lease lost for job %s; results rolled back", claim.job_id)
        return False
    except Exception:
        logger.exception("Pipeline execution failed for job %s", claim.job_id)
        with factory() as db:
            job = db.scalar(select(PipelineJobRow).where(
                PipelineJobRow.id == claim.job_id, PipelineJobRow.status == "RUNNING",
                PipelineJobRow.lease_token == claim.token).with_for_update())
            if job is not None:
                terminal = job.attempts >= MAX_ATTEMPTS
                job.status = "FAILED" if terminal else "READY"
                job.available_at = utc_now() + timedelta(seconds=2 ** job.attempts)
                job.last_error_code = "PIPELINE_RETRY_EXHAUSTED" if terminal else "PIPELINE_EXECUTION_ERROR"
                job.lease_token = None
                job.lease_until = None
                if terminal:
                    job.finished_at = utc_now()
                    mark_terminal(db, job.task_id, job.last_error_code)
                db.commit()
        return False


def run_once(factory, *, lease_seconds: float = 180, task_id: str | None = None) -> bool:
    claim = claim_job(factory, lease_seconds=lease_seconds, task_id=task_id)
    if claim is None:
        return False
    run_claim(factory, claim)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=1)
    parser.add_argument("--lease-seconds", type=float, default=180)
    args = parser.parse_args()
    if args.poll_interval < 0.1 or args.lease_seconds <= 0:
        parser.error("poll interval must be >= 0.1 and lease seconds must be positive")
    logging.basicConfig(level=logging.INFO)
    stopped = Event()
    for value in (signal.SIGINT, signal.SIGTERM):
        signal.signal(value, lambda *_: stopped.set())
    factory = session_factory_for()
    try:
        while not stopped.is_set():
            try:
                processed = run_once(factory, lease_seconds=args.lease_seconds)
            except Exception:
                logger.exception("Worker database operation failed")
                processed = False
            if args.once:
                break
            if not processed:
                stopped.wait(args.poll_interval)
    finally:
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    main()
