"""Create a local, untracked .env with random demo credentials; never overwrite it."""

from pathlib import Path
from secrets import token_hex, token_urlsafe


root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    raise SystemExit(".env already exists; no file was changed")

values = {
    "POSTGRES_PASSWORD": token_urlsafe(32),
    "MINIO_ROOT_USER": "qf" + token_hex(6),
    "MINIO_ROOT_PASSWORD": token_urlsafe(32),
    "DEFAULT_TENANT_ID": "demo-factory",
    "DEMO_MODE": "1",
}
for role in ("INSPECTOR", "ENGINEER", "MANAGER", "ADMIN", "AUDITOR"):
    values[f"DEMO_{role}_PASSWORD"] = token_urlsafe(24)
target.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
print("Created .env with randomly generated local demo credentials. Keep this file private.")
