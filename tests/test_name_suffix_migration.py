"""Taking the line item id back out of the names.

The id was appended to the name to tell two rows of the same product apart.
The table shows it in its own column now, so the suffix is the same number
twice on one row.

Run through real Alembic, because what is being checked is the SQL.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text

BEFORE = "0016_line_item_status"


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
    handle = f"sqlite:///{tmp_path}/names.db"
    _alembic(handle, BEFORE)
    engine = create_engine(handle)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO clients (id, name) VALUES (1, 'Ram Jack')"))
        conn.execute(
            text(
                "INSERT INTO orders (id, client_id, name, pacing_type, active, "
                "paused, terms_locked) VALUES (1,1,'#44807','impression',1,0,0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO line_items (id, order_id, name, external_id, "
                "sort_order, terms_locked, restricted) VALUES "
                "(1,1,'Display Ads · 101789','101789',0,0,0),"
                "(2,1,'Display Ads','106569',1,0,0),"
                # A name a buyer typed, which happens to carry the separator.
                "(3,1,'Spring push · phase two','107066',2,0,0),"
                # The separator, but not this row's id.
                "(4,1,'Display Ads · 101789','118056',3,0,0),"
                "(5,1,'No id at all · 12',NULL,4,0,0)"
            )
        )
    return handle


def _names(url):
    engine = create_engine(url)
    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT id, name FROM line_items")).all())


def test_the_rows_own_id_comes_off_its_name(url):
    _alembic(url, "head")
    assert _names(url)[1] == "Display Ads"


def test_a_name_without_one_is_left_alone(url):
    _alembic(url, "head")
    assert _names(url)[2] == "Display Ads"


def test_a_name_a_buyer_typed_is_not_mangled(url):
    """It carries the separator and is not an id."""
    _alembic(url, "head")
    assert _names(url)[3] == "Spring push · phase two"


def test_a_suffix_that_is_not_this_rows_id_stays(url):
    """Stripping it would claim to know what the number meant."""
    _alembic(url, "head")
    assert _names(url)[4] == "Display Ads · 101789"
    assert _names(url)[5] == "No id at all · 12"
