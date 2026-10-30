"""The details grid: one product, built the way the Details tab is kept by hand.

Run dates, a column per day of the flight, a row per strategy, then the
monthly and to-date running totals against even pacing - and the weekly
check that the Updates sheet records.
"""
from __future__ import annotations

import datetime as dt
import importlib
import os
import subprocess
import sys

import pytest


@pytest.fixture()
def site(tmp_path):
    url = f"sqlite:///{tmp_path}/details.db"
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
    import details
    import app as app_module

    importlib.reload(views)
    importlib.reload(details)
    importlib.reload(app_module)

    from models import Client, DailyDelivery, LineItem, Order

    with db_module.session_scope() as session:
        client = Client(name="Forrest Design Group")
        session.add(client)
        session.flush()
        order = Order(
            client_id=client.id,
            external_order_id="51200",
            name="Forrest Design Group #51200",
            pacing_type="impression",
            order_type="Insertion Order",
            status="IO Live",
            start_date=dt.date(2026, 10, 13),
            end_date=dt.date(2026, 11, 11),
        )
        session.add(order)
        session.flush()
        display = LineItem(
            order_id=order.id, external_id="99001", name="Display",
            product="Display Ads", sort_order=0,
            monthly_impressions=450_000.0, total_impressions=450_000.0,
            goal_cpm=2.04, sold_strategies="Behavioral, Retargeting",
        )
        session.add(display)
        session.flush()

        for day in range(13, 27):
            session.add(
                DailyDelivery(
                    date=dt.date(2026, 10, day),
                    data_source="StackAdapt",
                    campaign_id="CMP-9",
                    strategy_id=f"S-{day}",
                    client_name="Forrest Design Group",
                    external_order_id="51200",
                    external_line_item_id="99001",
                    product="Display Ads",
                    strategy_name="Display - Behavioral",
                    strategy_type="Behavioral",
                    impressions=10_000.0,
                    clicks=20.0,
                    cost=20.4,
                )
            )
        ids = (order.id, display.id)

    app_module.app.config["TESTING"] = True
    return app_module, ids


def test_the_grid_runs_the_whole_flight(site):
    app_module, (order_id, li_id) = site
    client = app_module.app.test_client()
    page = client.get(f"/orders/{order_id}/details/{li_id}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    # Opens on the month the data runs through.
    assert "13-Oct" in html and "31-Oct" in html and "11-Nov" not in html
    html = client.get(f"/orders/{order_id}/details/{li_id}?days=all").get_data(as_text=True)
    assert "13-Oct" in html and "11-Nov" in html
    assert "Build split" in html


def test_monthly_rows_start_again_on_the_first(site):
    import db as db_module
    import details

    _, (order_id, li_id) = site
    with db_module.session_scope() as session:
        d = details.details_view(session, order_id, li_id)

    assert len(d.dates) == 30
    # 14 days of 10,000 in October; the month row carries it, the to-date
    # row keeps going.
    assert d.month_actual[dt.date(2026, 10, 26)] == 140_000
    assert d.td_actual[dt.date(2026, 10, 26)] == 140_000
    # The to-date target reaches the sold total on the last day.
    assert d.td_target[dt.date(2026, 11, 11)] == pytest.approx(450_000)
    # And the monthly target starts again on 1 Nov.
    # November's goal over the 11 days of the flight in November.
    assert d.month_target[dt.date(2026, 11, 1)] == pytest.approx(450_000 / 11)
    assert d.day_target[dt.date(2026, 11, 1)] == pytest.approx(15_000)


def test_build_split_follows_the_setup_guide(site):
    """Retargeting takes a flat 10K a month; the rest goes to the others."""
    import db as db_module
    from models import StrategyTerms

    app_module, (order_id, li_id) = site
    client = app_module.app.test_client()
    client.post(f"/orders/{order_id}/details/{li_id}/build")
    client.post(f"/orders/{order_id}/details/{li_id}/build")

    with db_module.session_scope() as session:
        terms = {
            t.label: t
            for t in session.query(StrategyTerms).filter_by(line_item_id=li_id)
        }
    assert set(terms) == {"D - Behavioral", "D - Retargeting"}
    assert terms["D - Retargeting"].monthly_target == pytest.approx(10_000)
    assert terms["D - Behavioral"].monthly_target == pytest.approx(440_000)
    assert terms["D - Behavioral"].rate == 2.04

    html = client.get(f"/orders/{order_id}/details/{li_id}").get_data(as_text=True)
    assert "Build split" not in html
    assert "D - Behavioral" in html


def test_ctv_and_video_splits_two_thirds_to_video():
    import details
    from models import LineItem

    item = LineItem(
        name="CTV + Video", product="CTV + Video Ads",
        monthly_impressions=90_000.0, total_impressions=270_000.0,
        goal_cpm=7.0, sold_strategies="Behavioral, Retargeting",
    )
    plan = {p.label: p for p in details.plan_split(item, money=False)}
    video = sum(p.monthly for label, p in plan.items() if label.startswith("V "))
    ctv = sum(p.monthly for label, p in plan.items() if label.startswith("CTV "))
    assert video == pytest.approx(60_000)
    assert ctv == pytest.approx(30_000)
    assert "CTV - Retargeting" not in plan
    assert plan["V - Retargeting"].monthly == pytest.approx(10_000)
    assert plan["CTV - Behavioral"].total == pytest.approx(90_000)


def test_weekly_check_snapshots_pacing_and_logs_the_optimization(site):
    import db as db_module
    from models import DayNote, WeeklyCheck

    app_module, (order_id, li_id) = site
    client = app_module.app.test_client()
    client.post(
        f"/orders/{order_id}/details/{li_id}/check",
        data={
            "checked_on": "2026-10-27",
            "author": "Kiri",
            "tracking_conversions": "yes",
            "optimizations": "excluded gaming app categories",
        },
    )

    with db_module.session_scope() as session:
        check = session.query(WeeklyCheck).one()
        assert check.to_date == 140_000
        assert check.ctr == pytest.approx(0.002)
        assert check.tracking_conversions is True
        note = session.query(DayNote).one()
        assert note.line_item_id == li_id
        assert note.date == dt.date(2026, 10, 27)

    html = client.get(f"/orders/{order_id}/details/{li_id}").get_data(as_text=True)
    assert "excluded gaming app categories" in html
    assert "Tracking conversions - none reported" in html


def test_pages_are_served_from_memory_until_something_changes(site):
    """Rendered once; a save or a new file drops it."""
    import db as db_module
    import pagecache
    from models import IngestedFile, Order

    app_module, (order_id, li_id) = site
    app_module.app.config["TESTING"] = False
    app_module._warmer_started = True
    pagecache.clear()
    try:
        client = app_module.app.test_client()
        url = f"/orders/{order_id}"
        first = client.get(url).get_data(as_text=True)
        assert "Forrest Design Group #51200" in first

        # Changed underneath, with nothing posted: still the stored page.
        with db_module.session_scope() as session:
            session.get(Order, order_id).name = "Renamed #51200"
        assert client.get(url).get_data(as_text=True) == first

        # A new file lands: the stamp moves and the page is rebuilt.
        with db_module.session_scope() as session:
            session.add(IngestedFile(s3_key="orders/new.csv", kind="orders"))
        pagecache._stamp = None
        assert "Renamed #51200" in client.get(url).get_data(as_text=True)

        # Anything posted drops every stored page.
        with db_module.session_scope() as session:
            session.get(Order, order_id).name = "Again #51200"
        client.post(f"/orders/{order_id}/notes", data={"body": "x"})
        assert "Again #51200" in client.get(url).get_data(as_text=True)
    finally:
        app_module.app.config["TESTING"] = True
        pagecache.clear()


def test_a_dropped_connection_is_retried_once(site):
    """The database cutting the connection mid-page rebuilds it, not an error."""
    from sqlalchemy.exc import OperationalError

    import pagecache

    app_module, (order_id, li_id) = site
    app_module.app.config["TESTING"] = False
    app_module._warmer_started = True
    pagecache.clear()
    real = app_module.details.details_view
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("SELECT", {}, Exception("SSL SYSCALL error: EOF detected"))
        return real(*args, **kwargs)

    app_module.details.details_view = flaky
    try:
        page = app_module.app.test_client().get(f"/orders/{order_id}/details/{li_id}")
        assert page.status_code == 200
        assert len(calls) == 2
    finally:
        app_module.details.details_view = real
        app_module.app.config["TESTING"] = True
        pagecache.clear()


def test_budgeting_expand_loads_each_products_grid(site):
    app_module, (order_id, li_id) = site
    client = app_module.app.test_client()
    page = client.get(f"/orders/{order_id}").get_data(as_text=True)
    assert f"/orders/{order_id}/budgeting" in page
    assert "13-Oct" not in page  # the grids come with the expand, not the page

    grids = client.get(f"/orders/{order_id}/budgeting").get_data(as_text=True)
    assert "13-Oct" in grids and "11-Nov" not in grids
    assert "Build split" in grids


def test_budgeting_expand_steps_through_the_months(site):
    app_module, (order_id, li_id) = site
    client = app_module.app.test_client()
    grids = client.get(f"/orders/{order_id}/budgeting").get_data(as_text=True)
    assert 'data-q="month=2026-11"' in grids and "October 2026" in grids

    nov = client.get(f"/orders/{order_id}/budgeting?month=2026-11").get_data(as_text=True)
    assert "11-Nov" in nov and "13-Oct" not in nov
    assert 'data-q="month=2026-10"' in nov

    whole = client.get(f"/orders/{order_id}/budgeting?days=all").get_data(as_text=True)
    assert "13-Oct" in whole and "11-Nov" in whole


def test_strategy_targets_save_and_show_on_hover(site):
    import db as db_module
    from models import StrategyTerms

    app_module, (order_id, li_id) = site
    client = app_module.app.test_client()
    client.post(f"/orders/{order_id}/details/{li_id}/build")
    with db_module.session_scope() as session:
        term = session.query(StrategyTerms).filter_by(label="D - Behavioral").one()
        tid = term.id

    html = client.get(f"/orders/{order_id}/details/{li_id}").get_data(as_text=True)
    assert f'name="st-{tid}-label"' in html and "<select" in html
    assert "Demo settings" in html

    client.post(f"/orders/{order_id}/strategies/save", data={
        f"st-{tid}-label": "D - Behavioral",
        f"st-{tid}-targets": "Home Improvement\nInterior Design\n",
    })
    with db_module.session_scope() as session:
        assert session.get(StrategyTerms, tid).targets == "Home Improvement\nInterior Design"

    html = client.get(f"/orders/{order_id}/details/{li_id}").get_data(as_text=True)
    assert "Home Improvement&#10;Interior Design" in html
