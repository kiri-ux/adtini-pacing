"""Load client-serve drops from S3 into `daily_delivery`.

Each drop is a rolling window - the 2026-09-21 file carries every day back to
2026-08-22 - so days arrive many times over and later numbers supersede
earlier ones. Rows are therefore upserted on their grain rather than appended.
"""
from __future__ import annotations

import datetime as dt
import io
import logging
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db import engine, session_scope
from ingest import s3
from ingest.normalize import normalize
from models import DailyDelivery, IngestedFile

log = logging.getLogger(__name__)

CHUNK = 1000

# Everything except the grain and the primary key gets refreshed on conflict.
UPDATABLE = [
    "business_unit", "client_name", "external_order_id", "order_level_name",
    "line_item_name", "strategy_name", "strategy_type", "product",
    "campaign_name", "campaign_start_date", "impressions", "clicks", "cost",
    "conversions", "viewthroughs", "click_conversions", "goal_cpm",
]


@dataclass
class IngestResult:
    files_seen: int = 0
    files_loaded: int = 0
    files_skipped: int = 0
    rows_written: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{self.files_loaded} loaded",
            f"{self.files_skipped} already current",
            f"{self.rows_written:,} rows",
        ]
        if self.errors:
            parts.append(f"{len(self.errors)} failed")
        return ", ".join(parts)


def _insert(table):
    return sqlite_insert(table) if engine.dialect.name == "sqlite" else pg_insert(table)


def upsert_rows(session, frame: pd.DataFrame) -> int:
    """Write a normalized frame, replacing any row already on that grain."""
    if frame.empty:
        return 0

    records = frame.to_dict("records")
    written = 0
    table = DailyDelivery.__table__

    for start in range(0, len(records), CHUNK):
        batch = records[start : start + CHUNK]
        stmt = _insert(table).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["date", "data_source", "campaign_id", "strategy_id"],
            set_={col: getattr(stmt.excluded, col) for col in UPDATABLE},
        )
        session.execute(stmt)
        written += len(batch)

    return written


def load_bytes(session, raw_csv: bytes, source_label: str = "upload") -> tuple[int, pd.DataFrame]:
    """Parse and store one CSV's bytes. Returns (rows written, frame)."""
    # dtype=str keeps 17-digit Meta campaign ids out of float64, where they
    # lose their last digits. Numerics are coerced back in `normalize`.
    raw = pd.read_csv(io.BytesIO(raw_csv), dtype=str, low_memory=False)
    frame = normalize(raw)
    written = upsert_rows(session, frame)
    log.info("%s: %s rows -> %s stored", source_label, len(raw), written)
    return written, frame


def run(force: bool = False, limit: int | None = None) -> IngestResult:
    """Sweep the bucket and load anything new.

    A file already logged with the same ETag is skipped, so the sweep is safe
    to run on a schedule and safe to re-run after a failure.
    """
    result = IngestResult()

    try:
        objects = s3.list_objects()
    except Exception as exc:  # credentials, bucket policy, network
        result.errors.append(f"could not list s3: {exc}")
        return result

    result.files_seen = len(objects)
    if limit:
        objects = objects[-limit:]

    with session_scope() as session:
        seen = {
            row.s3_key: row
            for row in session.execute(select(IngestedFile)).scalars().all()
        }

        for obj in objects:
            prior = seen.get(obj.key)
            if prior and prior.etag == obj.etag and prior.status == "ok" and not force:
                result.files_skipped += 1
                continue

            try:
                raw_csv = s3.fetch_csv_bytes(obj.key)
                written, frame = load_bytes(session, raw_csv, obj.key)
            except Exception as exc:
                log.exception("ingest failed for %s", obj.key)
                result.errors.append(f"{obj.key}: {exc}")
                _log_file(session, prior, obj, status="error", message=str(exc))
                continue

            result.files_loaded += 1
            result.rows_written += written
            _log_file(
                session,
                prior,
                obj,
                status="ok",
                rows_read=len(frame),
                rows_written=written,
                min_date=frame["date"].min() if not frame.empty else None,
                max_date=frame["date"].max() if not frame.empty else None,
            )

    return result


def _log_file(
    session,
    prior: IngestedFile | None,
    obj: s3.S3Object,
    *,
    status: str,
    message: str | None = None,
    rows_read: int | None = None,
    rows_written: int | None = None,
    min_date: dt.date | None = None,
    max_date: dt.date | None = None,
) -> None:
    record = prior or IngestedFile(s3_key=obj.key)
    record.etag = obj.etag
    record.size_bytes = obj.size
    record.status = status
    record.message = message
    record.rows_read = rows_read
    record.rows_written = rows_written
    record.min_date = min_date
    record.max_date = max_date
    record.ingested_at = dt.datetime.utcnow()
    session.add(record)
    session.flush()
