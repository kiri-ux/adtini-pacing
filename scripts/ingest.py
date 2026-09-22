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
    for i, arg in enumerate(sys.argv):
        if arg == "--only" and i + 1 < len(sys.argv):
            only = sys.argv[i + 1]
        if arg == "--max-mb" and i + 1 < len(sys.argv):
            max_bytes = int(float(sys.argv[i + 1]) * 1_000_000)

    if only:
        log.info("loading one file: %s", only)
    result = loader.run(force="--force" in sys.argv, only=only, max_bytes=max_bytes)
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

    if result.rows_written:
        with session_scope() as session:
            log.info("order book: %s", adopt_unmatched_delivery(session).summary())

    # Loading nothing is fine; failing to read the bucket is not, and the
    # cron run should go red so it is noticed.
    return 1 if result.errors and not result.files_loaded else 0


if __name__ == "__main__":
    raise SystemExit(main())
