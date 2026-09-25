"""Loading a day of delivery from the order page.

A buyer chasing a number today should not have to wait for tonight's drop,
or load the whole book to see one order.
"""
from __future__ import annotations

import datetime as dt
import importlib
import io
import os
import subprocess
import sys

import pytest

HEADER = ",".join([
    "business_unit", "client", "impressions", "clicks", "ctr", "internal_cpm",
    "internal_cost", "campaign_name", "campaign_id", "campaign_start_date",
    "data_source_name", "date", "line_item_name", "line_item_id", "order_id",
    "order_level_name", "product", "strategy_id", "strategy_name",
    "strategy_type", "total_conversions", "viewthroughs", "click_conversions",
])


def row(order_id, line_item_id, day, impressions=1_000, strategy="S1"):
    return ",".join([
        "7 Mountains PA", "Acme", str(impressions), "8", "0.008", "4.50",
        "4.50", "Acme | Mobile", "CMP-1", "2026-08-01", "Mobile", day,
        "Acme - Mobile", line_item_id, order_id, "Acme #1", "Mobile",
        strategy, "MC - Geo-Fencing", "Geo-Fencing", "1", "0", "0",
    ])


@pytest.fixture()
def site(tmp_path):
    url = f"sqlite:///{tmp_path}/up.db"
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

    from models import Client, LineItem, Order

    with db_module.session_scope() as session:
        client = Client(name="Acme")
        session.add(client)
        session.flush()
        order = Order(
            client_id=client.id, external_order_id="44100", name="Acme #44100",
            pacing_type="impression", order_type="Insertion Order",
            status="IO Live",
            start_date=dt.date(2026, 8, 1), end_date=dt.date(2026, 12, 31),
        )
        session.add(order)
        session.flush()
        session.add(
            LineItem(
                order_id=order.id, external_id="88001", name="MC",
                product="Mobile Conquesting Display & Video Ads",
                monthly_impressions=100_000.0, total_impressions=500_000.0,
                goal_cpm=4.5,
            )
        )
        order_id = order.id

    app_module.app.config["TESTING"] = True
    return app_module, order_id


def _upload(app_module, order_id, text, name="client-serve.csv"):
    return app_module.app.test_client().post(
        f"/orders/{order_id}/delivery",
        data={"file": (io.BytesIO(text.encode()), name)},
        content_type="multipart/form-data",
        follow_redirects=True,
    ).get_data(as_text=True)


def test_a_day_can_be_loaded_from_the_order_page(site):
    from models import DailyDelivery

    import db as db_module

    app_module, order_id = site
    body = _upload(app_module, order_id, "\n".join([
        HEADER,
        row("44100", "88001", "2026-09-01", 1_200),
        row("44100", "88001", "2026-09-02", 1_300, strategy="S2"),
    ]))

    assert "Loaded 2 days" in body
    with db_module.session_scope() as session:
        rows = session.query(DailyDelivery).all()
        assert len(rows) == 2
        assert sum(r.impressions for r in rows) == 2_500


def test_only_this_orders_rows_are_kept(site):
    """A drop carrying the rest of the book must not arrive through a page
    that says it is about one order - nothing on the page would say it had."""
    from models import DailyDelivery

    import db as db_module

    app_module, order_id = site
    body = _upload(app_module, order_id, "\n".join([
        HEADER,
        row("44100", "88001", "2026-09-01", 1_200),
        row("99999", "77777", "2026-09-01", 5_000),
        row("99999", "77777", "2026-09-02", 5_000),
    ]))

    assert "Loaded 1 day" in body
    with db_module.session_scope() as session:
        rows = session.query(DailyDelivery).all()
        assert [r.external_order_id for r in rows] == ["44100"]


def test_a_file_with_nothing_for_this_order_says_so(site):
    from models import DailyDelivery

    import db as db_module

    app_module, order_id = site
    body = _upload(app_module, order_id, "\n".join([
        HEADER, row("99999", "77777", "2026-09-01"),
    ]))

    assert "Nothing in that file carries order 44100" in body
    with db_module.session_scope() as session:
        assert session.query(DailyDelivery).count() == 0


def test_re_uploading_the_same_day_replaces_it(site):
    """The grain is the day, so a corrected file corrects rather than doubles."""
    from models import DailyDelivery

    import db as db_module

    app_module, order_id = site
    _upload(app_module, order_id, "\n".join([
        HEADER, row("44100", "88001", "2026-09-01", 1_200)]))
    _upload(app_module, order_id, "\n".join([
        HEADER, row("44100", "88001", "2026-09-01", 1_450)]))

    with db_module.session_scope() as session:
        rows = session.query(DailyDelivery).all()
        assert len(rows) == 1
        assert rows[0].impressions == 1_450


def test_an_unreadable_file_does_not_take_the_page_down(site):
    app_module, order_id = site
    body = _upload(app_module, order_id, "this is not a delivery export at all")
    assert "could not be read" in body or "Nothing in that file" in body


def test_nothing_picked_is_said_plainly(site):
    app_module, order_id = site
    body = app_module.app.test_client().post(
        f"/orders/{order_id}/delivery", data={}, follow_redirects=True
    ).get_data(as_text=True)
    assert "Pick a file first" in body
