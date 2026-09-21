"""SQLAlchemy engine and session plumbing."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from config import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if _settings.is_sqlite else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Build the schema directly from the models, for tests only.

    Not for a real database: `create_all` creates missing tables but never
    alters existing ones, so on a live database it silently leaves new
    columns off and every query for one then fails. Alembic owns the schema
    everywhere else.
    """
    import models  # noqa: F401  (registers mappers)

    models.Base.metadata.create_all(engine)
