"""Explicit development-only seed data. No password is committed to the repository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import BatchRow, KnowledgeRow, UserRow
from .domain import AccountService, Role
from .config import database_url_from_env


DEMO_USERS = {
    "inspector.demo": ("林然", Role.INSPECTOR),
    "engineer.demo": ("陈工程师", Role.ENGINEER),
    "manager.demo": ("周宇", Role.MANAGER),
    "admin.demo": ("系统管理员", Role.ADMIN),
    "auditor.demo": ("王审计", Role.AUDITOR),
}


def seed_demo(db: Session, *, tenant_id: str, passwords: dict[str, str]) -> None:
    scenarios_path = Path(__file__).resolve().parents[2] / "data" / "scenarios.json"
    scenarios = json.loads(scenarios_path.read_text(encoding="utf-8"))
    for username, (display_name, role) in DEMO_USERS.items():
        password = passwords.get(username)
        if not password or len(password) < 12:
            raise ValueError(f"{username} requires an explicit password of at least 12 characters")
        existing = db.scalar(select(UserRow.id).where(UserRow.tenant_id == tenant_id,
                                                       UserRow.username == username))
        if existing:
            continue
        db.add(UserRow(id=str(uuid4()), tenant_id=tenant_id, username=username,
                       display_name=display_name, role=role.value,
                       password_hash=AccountService._hash_password(password),
                       active=True, must_change_password=False))
    start = datetime(2026, 10, 28, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    for index, scenario in enumerate(scenarios):
        if db.get(BatchRow, scenario["batch_id"]):
            continue
        batch_start = start + timedelta(hours=index * 2)
        db.add(BatchRow(id=scenario["batch_id"], tenant_id=tenant_id,
                        product_code="VALVE-A", equipment_id="EQ-07",
                        start_at=batch_start, end_at=batch_start + timedelta(hours=2),
                        scenario_id=scenario["id"]))
    knowledge = json.loads((scenarios_path.parent / "knowledge.json").read_text(encoding="utf-8"))
    for item in knowledge:
        identifier = f"{tenant_id}:{item['key']}:{item['version']}"
        if db.get(KnowledgeRow, identifier):
            continue
        db.add(KnowledgeRow(id=identifier, tenant_id=tenant_id, source_key=item["key"],
                            source_type=item["type"], product_code=item["product"],
                            version=item["version"], title=item["title"], content=item["content"],
                            content_hash=sha256(item["content"].encode()).hexdigest(),
                            valid_from=datetime.fromisoformat(item["valid_from"]),
                            valid_to=datetime.fromisoformat(item["valid_to"]) if item["valid_to"] else None,
                            active=True))
    db.commit()


def main() -> None:
    if os.environ.get("DEMO_MODE") != "1":
        raise SystemExit("Set DEMO_MODE=1 explicitly to load synthetic demo data")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = database_url_from_env()
    engine = create_engine(url)
    passwords = {name: os.environ.get(f"DEMO_{name.split('.')[0].upper()}_PASSWORD", "")
                 for name in DEMO_USERS}
    with sessionmaker(engine)() as db:
        seed_demo(db, tenant_id=os.environ.get("DEFAULT_TENANT_ID", "demo-factory"),
                  passwords=passwords)
    print("Seeded 12 synthetic scenarios and five explicitly configured demo accounts.")


if __name__ == "__main__":
    main()
