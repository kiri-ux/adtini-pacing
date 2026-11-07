#!/usr/bin/env python
"""Pull new drops from S3 and bring the order book up to date.

Run on a schedule (Render Cron Job) shortly after the daily files land, and
also started on demand by the Data page. Safe to run more than once a day: a
drop already loaded with the same ETag is skipped, and days that arrive again
simply replace themselves.

The schema is the web service's pre-deploy step to apply; this only reads and
writes rows.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Python puts the *script's* directory on sys.path, not the working
# directory, so `python scripts/ingest.py` cannot see the app beside it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings  # noqa: E402
from db import session_scope  # noqa: E402
from ingest import loader  # noqa: E402
from orderbook import adopt_unmatched_delivery  # noqa: E402
import views  # noqa: E402


def draft_splits(log: logging.Logger) -> None:
    """Give every running order without a split a drafted one.

    Same as the Data page's button, so a new order has rows to pace by the
    morning it lands. Only fills gaps; a split that exists is never touched.
    """
    import datetime as dt

    from sqlalchemy import func, select

    from models import IngestedFile

    try:
        with session_scope() as session:
            result = views.draft_missing_splits(session)
            log.info("splits: %s", result.summary())
            if result.strategies_added:
                # Cached pages are keyed on the newest load time; a page
                # rendered between the load and this would otherwise keep
                # showing those orders without a split.
                newest = session.execute(
                    select(IngestedFile)
                    .order_by(IngestedFile.ingested_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if newest is not None:
                    newest.ingested_at = dt.datetime.utcnow()
    except Exception:  # a failed draft must not turn the sweep red
        log.exception("drafting splits failed")


def sync_rz_orders(log: logging.Logger) -> bool:
    """Refresh open and recently ended orders straight from RZ.

    Skipped when the bot login is not set. Returns whether anything changed.
    """
    import os

    from ingest import rz_orders

    user = os.environ.get("RZ_USER", "").strip()
    password = os.environ.get("RZ_PASS", "")
    if not user or not password:
        log.info("rz orders: RZ_USER/RZ_PASS not set, skipped")
        return False
    try:
        clusters = [
            c.strip() for c in os.environ.get("RZ_CLUSTER_IDS", "").split(",")
            if c.strip()
        ]
        result = rz_orders.sync(
            rz_orders.RZClient(user, password), session_scope,
            cluster_ids=clusters or None,
        )
    except Exception:  # RZ being down must not turn the sweep red
        log.exception("rz orders sync failed")
        return False
    log.info("rz orders: %s", result.summary())
    for error in result.errors[:20]:
        log.warning("rz orders: %s", error)
    return result.changed


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("ingest")
    settings = get_settings()

    log.info(
        "sweep starting: bucket=%s prefix=%s",
        settings.s3_bucket,
        settings.s3_prefix,
    )

    only = None
    max_bytes = None
    kind = None
    for i, arg in enumerate(sys.argv):
        if arg == "--only" and i + 1 < len(sys.argv):
            only = sys.argv[i + 1]
        if arg == "--max-mb" and i + 1 < len(sys.argv):
            max_bytes = int(float(sys.argv[i + 1]) * 1_000_000)
        if arg == "--kind" and i + 1 < len(sys.argv):
            kind = sys.argv[i + 1]

    if "--rz-only" in sys.argv:
        if sync_rz_orders(log):
            draft_splits(log)
        return 0

    if only:
        log.info("loading one file: %s", only)
    if kind:
        log.info("only the %s files", kind)
    result = loader.run(
        force="--force" in sys.argv, only=only, max_bytes=max_bytes, kind=kind
    )
    log.info("sweep finished: %s", result.summary())
    for error in result.errors:
        log.error("%s", error)

    # A sweep that reaches the bucket and finds nothing looks identical from
    # the Data page to one that never reached it at all, so say which.
    if not result.files_seen and not result.errors:
        log.error(
            "no files found under s3://%s/%s - check the bucket name, the "
            "prefix, and that the AWS credentials are set on this service",
            settings.s3_bucket,
            settings.s3_prefix,
        )
        return 1

    # After the files, so a monthly export never overwrites a fresher read,
    # and before adopting, so new delivery joins its real order.
    rz_changed = False if only or kind == "delivery" else sync_rz_orders(log)

    if result.rows_written or rz_changed:
        with session_scope() as session:
            log.info("order book: %s", adopt_unmatched_delivery(session).summary())

    if result.files_loaded or rz_changed:
        draft_splits(log)

    # Loading nothing is fine; failing to read the bucket is not, and the
    # cron run should go red so it is noticed.
    return 1 if result.errors and not result.files_loaded else 0


if __name__ == "__main__":
    raise SystemExit(main())
