#!/usr/bin/env python
"""Pull new delivery drops from S3 and refresh the order book.

Run on a schedule (Render Cron Job) shortly after the daily files land. Safe
to run more than once a day: a drop already loaded with the same ETag is
skipped, and days that arrive again simply replace themselves.

The schema is the web service's pre-deploy step to apply; this only reads and
writes rows.
"""
from __future__ import annotations

import logging
import sys

from db import session_scope
from ingest import loader
from orderbook import sync_from_delivery


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    log = logging.getLogger("ingest")

    result = loader.run(force="--force" in sys.argv)
    log.info("s3 sweep: %s", result.summary())
    for error in result.errors:
        log.error("%s", error)

    if result.rows_written:
        with session_scope() as session:
            log.info("order book: %s", sync_from_delivery(session).summary())

    # A sweep that found the bucket but loaded nothing is fine; one that could
    # not read the bucket at all is not, and the cron run should go red.
    return 1 if result.errors and not result.files_loaded else 0


if __name__ == "__main__":
    raise SystemExit(main())
