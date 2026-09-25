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
REPAIR = "0008_repair_derived_terms"


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
    assert rows[1] == (600_000, 2.0, "rate card")
    assert rows[2] == (300_000, 10.0, "rate card")
    # A total that was already larger than one month of itself is left alone.
    assert rows[4] == (60_000, 2.5, "rate card")


def test_the_repair_leaves_hand_edited_terms_alone(url):
    _alembic(url, "head")
    assert _rows(url)[3] == (1.0, 9.99, None)


def test_a_total_with_nothing_to_rebuild_from_is_cleared(url):
    """A dash reads as unset. A wrong number reads as a fact."""
    _alembic(url, "head")
    total, cpm, _ = _rows(url)[5]
    assert total is None
    assert cpm == 2.0, "the CPM needs no dates, so it is still filled"


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
    # Only the repair again - later migrations add columns and would fail a
    # second time for reasons that have nothing to do with the repair.
    _alembic(url, REPAIR)

    assert _rows(url) == once


# --- the order's own notes move into the day log ---------------------------
BEFORE_LOG = "0014_note_line_item"


@pytest.fixture()
def notes_url(tmp_path):
    handle = f"sqlite:///{tmp_path}/notes.db"
    _alembic(handle, BEFORE_LOG)
    engine = create_engine(handle)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO clients (id, name) VALUES (1, 'Acme')"))
        conn.execute(
            text(
                "INSERT INTO orders (id, client_id, name, pacing_type, active, "
                "paused, terms_locked, start_date, notes, adjustment_note, "
                "last_adjusted_on) VALUES "
                "(1,1,'Both','impression',1,0,0,'2026-08-01',"
                "  'Client wants heavier Q4','Lowered budget','2026-09-12'),"
                "(2,1,'Note only','impression',1,0,0,'2026-08-01',"
                "  'Watch the CTR',NULL,NULL),"
                "(3,1,'Adjustment only','impression',1,0,0,'2026-08-01',"
                "  NULL,'Paused for holiday','2026-09-05'),"
                "(4,1,'Neither','impression',1,0,0,'2026-08-01',NULL,NULL,NULL),"
                "(5,1,'Blank','impression',1,0,0,'2026-08-01','','','2026-09-01')"
            )
        )
    return handle


def _notes(url):
    engine = create_engine(url)
    with engine.connect() as conn:
        return [
            (r[0], str(r[1]), r[2], r[3])
            for r in conn.execute(
                text(
                    "SELECT order_id, date, body, author FROM day_notes "
                    "ORDER BY order_id, id"
                )
            )
        ]


def test_the_orders_notes_become_dated_log_entries(notes_url):
    """Somebody typed them, so they are carried across rather than dropped."""
    _alembic(notes_url, "head")
    rows = _notes(notes_url)

    assert rows == [
        # The adjustment first, both on the day it was adjusted.
        (1, "2026-09-12", "Lowered budget", "from the order"),
        (1, "2026-09-12", "Client wants heavier Q4", "from the order"),
        # No adjustment date, so the day the flight started.
        (2, "2026-08-01", "Watch the CTR", "from the order"),
        (3, "2026-09-05", "Paused for holiday", "from the order"),
    ]


def test_an_order_with_nothing_written_on_it_gets_no_note(notes_url):
    """Including the ones holding an empty string rather than a null."""
    _alembic(notes_url, "head")
    carried = {row[0] for row in _notes(notes_url)}
    assert 4 not in carried
    assert 5 not in carried
