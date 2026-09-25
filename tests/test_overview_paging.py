"""Paging the homepage, and what it does and does not speed up.

Capping the rows shown cuts what the browser has to parse. It does not cut
what the server does: the list is sorted by how badly each order is pacing,
so every live order is computed whatever the page size. That is worth being
clear about, because it is the obvious thing to reach for and it is not the
lever it looks like.
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
    url = f"sqlite:///{tmp_path}/page.db"
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
        client = Client(name="Acme")
        session.add(client)
        session.flush()
        for n in range(1, 121):
            order = Order(
                client_id=client.id, external_order_id=str(20000 + n),
                name=f"Order {n}", pacing_type="impression",
                order_type="Insertion Order", status="IO Live",
                start_date=dt.date(2026, 8, 1), end_date=dt.date(2027, 6, 30),
            )
            session.add(order)
            session.flush()
            session.add(
                LineItem(
                    order_id=order.id, external_id=str(900000 + n), name="MC",
                    product="Mobile Conquesting Display & Video Ads",
                    monthly_impressions=100_000.0, total_impressions=600_000.0,
                    goal_cpm=4.5,
                )
            )
        session.add(
            DailyDelivery(
                date=dt.date(2026, 9, 1), data_source="Mobile", campaign_id="C1",
                strategy_id="S1", client_name="Acme",
                external_order_id="20001", external_line_item_id="900001",
                product="Mobile", strategy_name="MC - Geo-Fencing",
                impressions=1_000.0, clicks=5.0, cost=4.5,
            )
        )

    app_module.app.config["TESTING"] = True
    return app_module


def _window(body):
    match = re.search(r"<span>([\d]+)&ndash;\s*([\d]+)\s*of ([\d,]+)</span>", body)
    assert match, "the pager should name the rows it is showing"
    return tuple(int(part.replace(",", "")) for part in match.groups())


def _rows(body):
    return len(re.findall(r'href="/orders/\d+"', body))


def test_the_default_page_is_fifty_rows(site):
    body = site.app.test_client().get("/").get_data(as_text=True)
    assert _window(body) == (1, 50, 120)


def test_the_page_size_can_be_changed(site):
    client = site.app.test_client()
    for size, expected in ((25, (1, 25, 120)), (100, (1, 100, 120))):
        body = client.get(f"/?rows={size}").get_data(as_text=True)
        assert _window(body) == expected


def test_the_window_matches_the_page_size(site):
    """It was written out as 150 whatever the size actually was, so it named
    rows the page was not showing."""
    client = site.app.test_client()

    body = client.get("/?rows=25&page=3").get_data(as_text=True)
    assert _window(body) == (51, 75, 120)

    body = client.get("/?rows=100&page=2").get_data(as_text=True)
    assert _window(body) == (101, 120, 120)


def test_an_unknown_page_size_falls_back(site):
    client = site.app.test_client()
    for bad in ("999", "0", "-5", "lots"):
        assert _window(client.get(f"/?rows={bad}").get_data(as_text=True)) == (
            1, 50, 120
        )


def test_a_smaller_page_sends_less_to_the_browser(site):
    """Which is what capping the rows actually buys."""
    client = site.app.test_client()
    small = len(client.get("/?rows=25").get_data())
    large = len(client.get("/?rows=100").get_data())
    assert small < large / 2


def test_every_live_order_is_still_counted(site):
    """The count is the whole filtered list, not the page.

    Sorting by how badly an order is pacing needs all of them, which is why
    a smaller page does not make the server do less.
    """
    client = site.app.test_client()
    for size in (25, 50, 100, 250):
        assert _window(client.get(f"/?rows={size}").get_data(as_text=True))[2] == 120
