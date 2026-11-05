"""Database connection configuration without unsafe URL password interpolation."""

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


def database_url_from_env() -> str | URL:
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit
    host = os.environ.get("DB_HOST")
    if host:
        return URL.create(
            "postgresql+psycopg",
            username=os.environ.get("DB_USER", "qualityflow"),
            password=os.environ.get("DB_PASSWORD", ""),
            host=host,
            port=int(os.environ.get("DB_PORT", "5432")),
            database=os.environ.get("DB_NAME", "qualityflow"),
        )
    return "sqlite:///./qualityflow.db"
