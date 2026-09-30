"""Rows a newer report no longer mentions.

The delivery merge replaces per grain - (date, source, campaign, strategy) -
not per day. Two reports covering the same days do not double count. But a
strategy that stops mid-flight and drops out of later reports, rather than
reporting zeroes, leaves its rows in place and the day reads high by
whatever it last served.

Counted, never deleted. Deleting on the strength of a report that may be
partial is how real delivery gets wiped; the count is what says whether the
case happens at all.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys

import pytest

HEAD = (
    "date,data_source,campaign_id,strategy_id,client_name,order_id,"
    "line_item_id,product,strategy_name,impressions,clicks,cost\n"
)


def row(day, strategy, impressions, campaign="CMP-1", order="44807"):
    return (
        f"2026-09-{day:02d},Display,{campaign},{strategy},Ram Jack,{order},"
        f"101789,Display Ads,D - {strategy},{impressions},5,3.00\n"
    )


@pytest.fixture()
def load(tmp_path):
    url = f"sqlite:///{tmp_path}/stale.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True,
    )
    os.environ["DATABASE_URL"] = url

    import config

    config.get_settings.cache_clear()
    import db as db_module

    importlib.reload(db_module)
    from ingest import loader

    importlib.reload(loader)

    def _load(body, label="report"):
        with db_module.session_scope() as session:
            _, summary = loader.load_bytes(session, (HEAD + body).encode(), label)
        return summary

    return _load


def test_a_first_load_has_nothing_stale(load):
    summary = load(row(1, "RT", 1000) + row(2, "RT", 1000))

    assert summary["stale_rows"] == 0


def test_a_report_repeating_what_is_there_has_nothing_stale(load):
    """Overlapping reports are the normal case and are already safe: the
    merge replaces the grain rather than adding to it."""
    load(row(1, "RT", 1000) + row(2, "RT", 1000))
    summary = load(row(2, "RT", 1000) + row(3, "RT", 1000))

    assert summary["stale_rows"] == 0


def test_a_strategy_dropping_out_is_counted(load):
    """Report A has Retargeting and Categories on 1 Sep. Report B covers the
    same day with Retargeting only, so the day still reads 1,500."""
    load(row(1, "RT", 1000) + row(1, "CAT", 500))
    summary = load(row(1, "RT", 1000))

    assert summary["stale_rows"] == 1
    assert summary["stale_impressions"] == 500


def test_a_day_the_file_does_not_cover_is_left_out_of_the_count(load):
    """A report about this week says nothing about last week."""
    load(row(1, "RT", 1000) + row(2, "CAT", 500))
    summary = load(row(1, "RT", 1000))

    assert summary["stale_rows"] == 0, "2 Sep is not this file's to speak for"


def test_another_campaign_on_the_same_day_is_left_out(load):
    """Scoped to the campaign-days the file covers, so a report about one
    order says nothing about any other. This is what makes the count safe
    to act on later."""
    load(row(1, "RT", 1000, campaign="CMP-1") + row(1, "RT", 500, campaign="CMP-2"))
    summary = load(row(1, "RT", 1000, campaign="CMP-1"))

    assert summary["stale_rows"] == 0


def test_the_stale_rows_are_still_there_afterwards(load):
    """Counted, never deleted."""
    import db as db_module
    from models import DailyDelivery
    from sqlalchemy import func, select

    load(row(1, "RT", 1000) + row(1, "CAT", 500))
    load(row(1, "RT", 1000))

    with db_module.session_scope() as session:
        total = session.execute(
            select(func.sum(DailyDelivery.impressions))
        ).scalar()
    assert total == 1500


def test_the_count_survives_onto_the_file_log(load):
    """It is read off the Data page, not the sweep's stdout."""
    import db as db_module
    from ingest import loader
    from models import IngestedFile
    from sqlalchemy import select

    load(row(1, "RT", 1000) + row(1, "CAT", 500))
    summary = load(row(1, "RT", 1000))

    class Obj:
        key, etag, size = "orders/client-serve_b.csv", "e1", 10

    with db_module.session_scope() as session:
        loader._log_file(
            session, Obj(), kind=loader.DELIVERY, status="ok",
            stale_rows=summary["stale_rows"],
            stale_impressions=summary["stale_impressions"],
        )
    with db_module.session_scope() as session:
        record = session.execute(select(IngestedFile)).scalar_one()
        assert record.stale_rows == 1
        assert record.stale_impressions == 500
