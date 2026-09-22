"""The deploy-time repair of the two derived sold terms.

The setup CPM is a rate card lookup and the sold total is the monthly figure
over the months the line item runs. Neither comes from the orders export, so
neither can be fixed by re-reading one - and a sweep skips a file it has
already read anyway. The repair runs on deploy rather than behind a button,
because a button is a thing to remember and a thing to get wrong.

Run through real Alembic against a real database, because what is being
checked is the SQL, and the SQL is written twice - once for Postgres and
once for SQLite, which do not share a spelling for pulling a month out of a
date.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text

BEFORE = "0007_delivery_campaign_index"


def _alembic(url: str, target: str) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr


@pytest.fixture()
def url(tmp_path):
    handle = f"sqlite:///{tmp_path}/repair.db"
    _alembic(handle, BEFORE)
    engine = create_engine(handle)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO clients (id, name) VALUES (1, 'Acme')"))
        conn.execute(
            text(
                "INSERT INTO orders (id, client_id, name, pacing_type, active, "
                "paused, terms_locked, start_date, end_date) VALUES "
                "(1,1,'Six months','impression',1,0,0,'2026-08-01','2027-01-31'),"
                "(2,1,'No dates','impression',1,0,0,NULL,NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO line_items (id, order_id, name, product, sort_order, "
                "terms_locked, restricted, monthly_impressions, total_impressions, "
                "goal_cpm) VALUES "
                # The ratio artifact the old parse stored.
                "(1,1,'Display','Display Ads',0,0,0,100000,0.999999999999,NULL),"
                # A month count where the total belongs.
                "(2,1,'CTV','Connected TV Ads',1,0,0,50000,6,NULL),"
                # Edited by hand: must not be touched, however wrong it looks.
                "(3,1,'Hand','Display Ads',2,1,0,100000,1,9.99),"
                # Already right: the total stays, the missing CPM is filled.
                "(4,1,'Fine','Social Mirror Ads',3,0,0,10000,60000,NULL),"
                # Nothing to rebuild from.
                "(5,2,'Orphan','Display Ads',0,0,0,100000,1,NULL)"
            )
        )
    return handle


def _rows(url):
    engine = create_engine(url)
    with engine.connect() as conn:
        return {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute(
                text(
                    "SELECT id, total_impressions, goal_cpm, goal_cpm_source "
                    "FROM line_items"
                )
            )
        }


def test_the_repair_rebuilds_totals_and_fills_cpms(url):
    _alembic(url, "head")
    rows = _rows(url)

    # Six months of the monthly figure, from the flight's own dates.
    assert rows[1] == (600_000, 2.5, "rate card")
    assert rows[2] == (300_000, 14.0, "rate card")
    # A total that was already larger than one month of itself is left alone.
    assert rows[4] == (60_000, 3.0, "rate card")


def test_the_repair_leaves_hand_edited_terms_alone(url):
    _alembic(url, "head")
    assert _rows(url)[3] == (1.0, 9.99, None)


def test_a_total_with_nothing_to_rebuild_from_is_cleared(url):
    """A dash reads as unset. A wrong number reads as a fact."""
    _alembic(url, "head")
    total, cpm, _ = _rows(url)[5]
    assert total is None
    assert cpm == 2.5, "the CPM needs no dates, so it is still filled"


def test_running_the_repair_twice_changes_nothing(url):
    """It runs on every deploy that has not seen it; it must be harmless."""
    _alembic(url, "head")
    once = _rows(url)

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE alembic_version SET version_num = :before"),
            {"before": BEFORE},
        )
    _alembic(url, "head")

    assert _rows(url) == once
