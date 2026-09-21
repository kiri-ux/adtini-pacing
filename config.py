"""Runtime configuration, read once from the environment."""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.database_url = os.getenv("DATABASE_URL") or "sqlite:///./pacing.db"
        # Render hands out `postgres://`, which SQLAlchemy 2 no longer accepts.
        if self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace("postgres://", "postgresql://", 1)

        self.s3_bucket = os.getenv("S3_BUCKET", "adtini-orders")
        self.s3_prefix = os.getenv("S3_PREFIX", "orders/")
        self.aws_region = os.getenv("AWS_REGION", "us-east-1")

        self.app_password = os.getenv("APP_PASSWORD", "")
        self.session_secret = os.getenv("SESSION_SECRET", "dev-secret")
        self.ingest_on_start = _bool("INGEST_ON_START", False)
        self.build = os.getenv("RENDER_GIT_COMMIT", "")[:7]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
