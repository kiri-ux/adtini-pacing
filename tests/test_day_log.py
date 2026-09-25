"""The running commentary beside the delivery, and the grid it explains.

Without a note a dip in the daily grid is unexplainable a month later. The
hand-kept sheets have always carried one; this is that, kept per day so it
lines up with the delivery it explains.
"""
from __future__ import annotations

import datetime as dt
import importlib
import os
import re
import subprocess
import sys

import pytest


@pytest.fixture()
def site(tmp_path):
    url = f"sqlite:///{tmp_path}/log.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )
    os.environ["DATABASE_URL"] = url
    os.environ["APP_PASSWORD"] = ""

    import config

    config.get_settings.cache_clear()
    import db as db_module

    importlib.reload(db_module)
    import views
    import app as app_module

    importlib.reload(views)
    importlib.reload(app_module)

    from models import Client, DailyDelivery, LineItem, Order

    with db_module.session_scope() as session:
        client = Client(name="Peters")
        session.add(client)
        session.flush()
        order = Order(
            client_id=client.id, external_order_id="51741",
            name="Peters #51741", pacing_type="impression",
            order_type="Insertion Order", status="IO Live",
            start_date=dt.date(2026, 1, 20), end_date=dt.date(2026, 11, 30),
        )
        session.add(order)
        session.flush()
        session.add(
            LineItem(
                order_id=order.id, external_id="122628", name="MC",
                product="Mobile Conquesting Display & Video Ads",
                monthly_impressions=100_000.0, total_impressions=300_000.0,
                goal_cpm=4.5,
            )
        )
        session.flush()
        for month, days in ((8, 31), (9, 21)):
            for day in range(1, days + 1):
                session.add(
                    DailyDelivery(
                        date=dt.date(2026, month, day),
                        data_source="Mobile", campaign_id="C1",
                        strategy_id=f"{month}-{day}",
                        client_name="Peters", external_order_id="51741",
                        external_line_item_id="122628",
                        product="Mobile Conquesting Display & Video Ads",
                        strategy_name="MC - Geo-Fencing",
                        impressions=1_200.0, clicks=6.0, cost=5.4,
                    )
                )
        order_id = order.id

    app_module.app.config["TESTING"] = True
    return app_module, order_id


def _days(body):
    return re.findall(r'<th class="num">(\d+-\w+)</th>', body)


def test_a_note_can_be_written_against_a_day(site):
    from models import DayNote

    import db as db_module

    app_module, order_id = site
    app_module.app.test_client().post(
        f"/orders/{order_id}/notes",
        data={
            "date": "2026-09-10",
            "body": "Lowered budget after the holiday dip",
            "author": "Kiri",
        },
        follow_redirects=True,
    )

    with db_module.session_scope() as session:
        note = session.query(DayNote).one()
        assert note.date == dt.date(2026, 9, 10)
        assert note.body == "Lowered budget after the holiday dip"
        assert note.author == "Kiri"


def test_an_empty_note_is_not_saved(site):
    from models import DayNote

    import db as db_module

    app_module, order_id = site
    app_module.app.test_client().post(
        f"/orders/{order_id}/notes",
        data={"date": "2026-09-10", "body": "   "},
        follow_redirects=True,
    )
    with db_module.session_scope() as session:
        assert session.query(DayNote).count() == 0


def test_the_log_reads_newest_first_and_marks_its_day_on_the_grid(site):
    import db as db_module
    import views

    app_module, order_id = site
    client = app_module.app.test_client()
    for day, body in (("2026-09-03", "Creative swapped"),
                      ("2026-09-10", "Lowered budget")):
        client.post(
            f"/orders/{order_id}/notes",
            data={"date": day, "body": body},
            follow_redirects=True,
        )

    with db_module.session_scope() as session:
        log = views.day_log(session, order_id)
    assert [n.body for n in log] == ["Lowered budget", "Creative swapped"]

    body = client.get(f"/orders/{order_id}").get_data(as_text=True)
    assert body.count("notemark") == 2, "a mark on each day that has one"
    assert "Lowered budget" in body


def test_a_note_is_removable(site):
    from models import DayNote

    import db as db_module

    app_module, order_id = site
    client = app_module.app.test_client()
    client.post(
        f"/orders/{order_id}/notes",
        data={"date": "2026-09-10", "body": "Typo"},
        follow_redirects=True,
    )
    with db_module.session_scope() as session:
        note_id = session.query(DayNote).one().id

    client.post(
        f"/orders/{order_id}/notes/{note_id}/delete", follow_redirects=True
    )
    with db_module.session_scope() as session:
        assert session.query(DayNote).count() == 0


# --- the grid shows the days worth looking at ------------------------------
def test_the_grid_shows_this_month_by_default(site):
    """A year-long flight is hundreds of columns and the interesting ones
    are always at the end."""
    app_module, order_id = site
    days = _days(
        app_module.app.test_client().get(f"/orders/{order_id}").get_data(as_text=True)
    )
    assert days[0] == "1-Sep"
    assert days[-1] == "21-Sep"
    assert len(days) == 21


def test_the_grid_range_can_be_widened(site):
    app_module, order_id = site
    client = app_module.app.test_client()

    thirty = _days(client.get(f"/orders/{order_id}?grid=30").get_data(as_text=True))
    assert len(thirty) == 30
    assert (thirty[0], thirty[-1]) == ("23-Aug", "21-Sep")

    flight = _days(
        client.get(f"/orders/{order_id}?grid=flight").get_data(as_text=True)
    )
    assert (flight[0], flight[-1]) == ("1-Aug", "21-Sep")

    # Anything unrecognised falls back to the month rather than erroring.
    assert len(_days(
        client.get(f"/orders/{order_id}?grid=nonsense").get_data(as_text=True)
    )) == 21


def test_the_row_labels_are_pinned(site):
    """Scrolled, an unpinned grid showed numbers with nothing saying which
    row they belonged to."""
    app_module, order_id = site
    body = app_module.app.test_client().get(f"/orders/{order_id}").get_data(
        as_text=True
    )
    assert 'class="pin"' in body
    # Row labels are header cells, so a screen reader announces them too.
    assert re.search(r'<tr class="gridproduct"><th class="pin">', body)
    assert re.search(r'<tr class="gridstrategy"><th class="pin">', body)
