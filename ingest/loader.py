"""Load the S3 drops.

Two kinds land in the same prefix and are told apart by filename:

* `client-serve*` - delivery. Each drop is a rolling window (the 2026-09-21
  file carries every day back to 2026-08-22), so days arrive many times over
  and later numbers supersede earlier ones. Rows are upserted on their grain
  rather than appended.
* `orders*` - the sold side, upserted into the order book.
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
from ingest import orders as orders_file
from ingest import s3
from ingest.normalize import normalize
from models import DailyDelivery, IngestedFile

DELIVERY = "delivery"
ORDERS = "orders"


def classify(key: str) -> str | None:
    """Which kind of drop a key is, from its filename.

    Returns None for anything unrecognised, which is skipped rather than
    guessed at - a new export landing in this prefix should be noticed, not
    half-read into the wrong table.
    """
    name = key.rsplit("/", 1)[-1].lower()
    if name.startswith("client-serve"):
        return DELIVERY
    if name.startswith("orders"):
        return ORDERS
    return None

log = logging.getLogger(__name__)

CHUNK = 1000

# Everything except the grain and the primary key gets refreshed on conflict.
UPDATABLE = [
    "business_unit", "client_name", "external_order_id", "order_level_name",
    "line_item_name", "strategy_name", "strategy_type", "product", "restricted",
    "campaign_name", "campaign_start_date", "impressions", "clicks", "cost",
    "conversions", "viewthroughs", "click_conversions", "goal_cpm",
]


@dataclass
class IngestResult:
    files_seen: int = 0
    files_loaded: int = 0
    files_skipped: int = 0
    files_unknown: int = 0
    rows_written: int = 0
    delivery_loaded: int = 0
    orders_loaded: int = 0
    order_book: str | None = None
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{self.delivery_loaded} delivery and {self.orders_loaded} orders files loaded",
            f"{self.files_skipped} already current",
            f"{self.rows_written:,} delivery rows",
        ]
        if self.files_unknown:
            parts.append(f"{self.files_unknown} unrecognised")
        if self.order_book:
            parts.append(self.order_book)
        if self.errors:
            parts.append(f"{len(self.errors)} failed")
        return "; ".join(parts)


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
    """Parse and store one delivery CSV's bytes. Returns (rows written, frame)."""
    # dtype=str keeps 17-digit Meta campaign ids out of float64, where they
    # lose their last digits. Numerics are coerced back in `normalize`.
    raw = pd.read_csv(io.BytesIO(raw_csv), dtype=str, low_memory=False)
    frame = normalize(raw)
    written = upsert_rows(session, frame)
    log.info("%s: %s rows -> %s stored", source_label, len(raw), written)
    return written, frame


def load_orders_bytes(session, raw_csv: bytes, source_label: str = "upload"):
    """Parse and store one orders CSV's bytes.

    Returns (import result, normalized frame) - the frame carries which
    columns were recognised, for the Data page.
    """
    from orderbook import import_orders

    raw = pd.read_csv(io.BytesIO(raw_csv), dtype=str, low_memory=False)
    frame = orders_file.normalize(raw)
    result = import_orders(session, frame)
    log.info("%s: %s rows -> %s", source_label, len(frame.rows), result.summary())
    return result, frame


def run(force: bool = False, limit: int | None = None) -> IngestResult:
    """Sweep the bucket and load anything new.

    Orders are loaded before delivery so a line item exists for delivery to
    join to on the same sweep. A file already logged with the same ETag is
    skipped, so the sweep is safe on a schedule and safe to re-run.
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

    # Orders first, then delivery, each oldest key first so later drops win.
    ordered: list[tuple[str, s3.S3Object]] = []
    for kind in (ORDERS, DELIVERY):
        ordered += [(kind, o) for o in objects if classify(o.key) == kind]
    result.files_unknown = len(objects) - len(ordered)

    with session_scope() as session:
        seen = {
            row.s3_key: row
            for row in session.execute(select(IngestedFile)).scalars().all()
        }

        for kind, obj in ordered:
            prior = seen.get(obj.key)
            if prior and prior.etag == obj.etag and prior.status == "ok" and not force:
                result.files_skipped += 1
                continue

            try:
                raw_csv = s3.fetch_csv_bytes(obj.key)
                if kind == DELIVERY:
                    written, frame = load_bytes(session, raw_csv, obj.key)
                    result.delivery_loaded += 1
                    result.rows_written += written
                    _log_file(
                        session, prior, obj, kind=kind, status="ok",
                        rows_read=len(frame), rows_written=written,
                        min_date=frame["date"].min() if not frame.empty else None,
                        max_date=frame["date"].max() if not frame.empty else None,
                    )
                else:
                    imported, frame = load_orders_bytes(session, raw_csv, obj.key)
                    result.orders_loaded += 1
                    _log_file(
                        session, prior, obj, kind=kind, status="ok",
                        rows_read=len(frame.rows),
                        rows_written=imported.line_items_added + imported.line_items_updated,
                        message=imported.summary(),
                        unmapped=frame.unmapped_note,
                    )
            except Exception as exc:
                log.exception("ingest failed for %s", obj.key)
                result.errors.append(f"{obj.key}: {exc}")
                _log_file(session, prior, obj, kind=kind, status="error", message=str(exc))
                continue

            result.files_loaded += 1

        # Delivery with no order record still has to be visible.
        if result.rows_written:
            from orderbook import adopt_unmatched_delivery

            result.order_book = adopt_unmatched_delivery(session).summary()

    return result


def _log_file(
    session,
    prior: IngestedFile | None,
    obj: s3.S3Object,
    *,
    kind: str,
    status: str,
    message: str | None = None,
    rows_read: int | None = None,
    rows_written: int | None = None,
    min_date: dt.date | None = None,
    max_date: dt.date | None = None,
    unmapped: str | None = None,
) -> None:
    record = prior or IngestedFile(s3_key=obj.key)
    record.kind = kind
    record.unmapped_columns = unmapped
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
