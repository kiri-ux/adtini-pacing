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
    # The daily goal is recalculated each day from what is left: November
    # starts fresh against its own goal over its 11 days.
    assert d.day_target[dt.date(2026, 11, 1)] == pytest.approx(450_000 / 11)
    # October started on the 13th with nothing served before it, then ran
    # 10,000 a day: by the 27th what is left is spread over the 5 days left.
    assert d.day_target[dt.date(2026, 10, 13)] == pytest.approx(450_000 / 19)
    assert d.day_target[dt.date(2026, 10, 27)] == pytest.approx(
        (450_000 - 140_000) / 5
    )


def test_build_split_follows_the_setup_guide(site):
    """Retargeting takes 5% of the monthly goal; the rest goes to the others."""
    import db as db_module
    from models import StrategyTerms

    from models import DailyDelivery

    app_module, (order_id, li_id) = site
    # Nothing delivered yet, so the guide decides the split.
    with db_module.session_scope() as session:
        session.query(DailyDelivery).delete()
    client = app_module.app.test_client()
    client.post(f"/orders/{order_id}/details/{li_id}/build")
    client.post(f"/orders/{order_id}/details/{li_id}/build")

    with db_module.session_scope() as session:
        terms = {
            t.label: t
            for t in session.query(StrategyTerms).filter_by(line_item_id=li_id)
        }
    assert set(terms) == {"D - Behavioral", "D - Retargeting"}
    assert terms["D - Retargeting"].monthly_target == pytest.approx(22_500)
    assert terms["D - Behavioral"].monthly_target == pytest.approx(427_500)
    assert terms["D - Behavioral"].rate == 2.04

    html = client.get(f"/orders/{order_id}/details/{li_id}").get_data(as_text=True)
    assert "Build split" not in html
    assert "D - Behavioral" in html


def test_build_split_keeps_a_bought_strategy_that_is_not_running(site):
    """Only Behavioral is serving; the order also bought Retargeting."""
    import db as db_module
    from models import StrategyTerms

    app_module, (order_id, li_id) = site
    app_module.app.test_client().post(f"/orders/{order_id}/details/{li_id}/build")
    with db_module.session_scope() as session:
        terms = {
            t.match_key: t.monthly_target
            for t in session.query(StrategyTerms).filter_by(line_item_id=li_id)
        }
    assert terms == {
        "behavioral": pytest.approx(427_500),
        "retargeting": pytest.approx(22_500),
    }


def test_build_split_follows_serve_when_the_order_lists_none(site):
    import db as db_module
    from models import LineItem, StrategyTerms

    app_module, (order_id, li_id) = site
    with db_module.session_scope() as session:
        session.get(LineItem, li_id).sold_strategies = None
    app_module.app.test_client().post(f"/orders/{order_id}/details/{li_id}/build")
    with db_module.session_scope() as session:
        terms = session.query(StrategyTerms).filter_by(line_item_id=li_id).all()
        assert [(t.match_key, t.monthly_target) for t in terms] == [
            ("behavioral", pytest.approx(450_000))
        ]


def test_draft_missing_splits_uses_the_order(site):
    import db as db_module
    import views
    from models import StrategyTerms

    app_module, (order_id, li_id) = site
    with db_module.session_scope() as session:
        views.draft_missing_splits(session, as_of=dt.date(2026, 10, 26))
    with db_module.session_scope() as session:
        keys = {
            t.match_key
            for t in session.query(StrategyTerms).filter_by(line_item_id=li_id)
        }
    assert keys == {"behavioral", "retargeting"}


def test_a_bought_strategy_shows_before_it_runs(site):
    import details
    import db as db_module

    app_module, (order_id, li_id) = site
    with db_module.session_scope() as session:
        d = details.details_view(session, order_id, li_id)
        rows = {e.label: e for e in d.elements}
    assert set(rows) == {"D - Behavioral", "D - Retargeting"}
    assert rows["D - Retargeting"].ordered and not rows["D - Retargeting"].running
    assert rows["D - Behavioral"].running

    html = app_module.app.test_client().get(
        f"/orders/{order_id}/details/{li_id}"
    ).get_data(as_text=True)
    assert "Last serve: n/a" in html
    assert "not sold" not in html


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
    # 5% of the product's 90K, not of the video half's 60K.
    assert plan["V - Retargeting"].monthly == pytest.approx(4_500)
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
        # The last copy is served at once, and a fresh one queued behind it.
        assert client.get(url).get_data(as_text=True) == first
        assert pagecache._wanted.get_nowait().startswith(url)
        refreshed = client.get(url, headers={pagecache.REFRESH_HEADER: "1"})
        assert "Renamed #51200" in refreshed.get_data(as_text=True)
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
    assert "<li>Home Improvement</li><li>Interior Design</li>" in html


def test_running_cpm_is_last_weeks_internal_cost_against_the_card(site):
    import db as db_module
    import details

    app_module, (order_id, li_id) = site
    with db_module.session_scope() as session:
        d = details.details_view(session, order_id, li_id, as_of=dt.date(2026, 10, 26))
        # 20.40 cost per 10,000 impressions every day = $2.04 CPM.
        assert d.run_cpm == pytest.approx(2.04)
        assert all(e.run_cpm == pytest.approx(2.04) for e in d.elements if e.impressions)
        # Display: starting $1.00, max $2.50 on the card.
        assert d.card is not None and d.card.max_cpm == 2.50

    html = app_module.app.test_client().get(
        f"/orders/{order_id}/details/{li_id}").get_data(as_text=True)
    assert "Run. CPM" in html and "$2.04" in html


def test_running_cpm_only_counts_the_window():
    pts = [(dt.date(2026, 11, 1), 1000, 3.0)] + [
        (dt.date(2026, 11, d), 1000, 1.5) for d in range(3, 10)
    ]
    # Nov 3-9 is the 7-day window ending Nov 9; Nov 1 drops out.
    assert details_mod().run_cpm(pts, dt.date(2026, 11, 9)) == pytest.approx(1.5)
    assert details_mod().run_cpm([], dt.date(2026, 11, 9)) is None


def details_mod():
    import details
    return details


def test_an_unmatched_line_links_to_a_strategy_and_unlinks(site):
    import details
    import db as db_module
    from models import DailyDelivery, StrategyTerms

    app_module, (order_id, li_id) = site
    with db_module.session_scope() as session:
        session.add(
            DailyDelivery(
                date=dt.date(2026, 10, 20), data_source="StackAdapt",
                campaign_id="CMP-9", strategy_id="S-X",
                client_name="Forrest Design Group", external_order_id="51200",
                external_line_item_id="99001", product="Display Ads",
                strategy_name="Forrest Site Visitors Pool",
                impressions=3_000.0, clicks=1.0, cost=6.0,
            )
        )
    client = app_module.app.test_client()
    client.post(f"/orders/{order_id}/details/{li_id}/build")

    with db_module.session_scope() as session:
        d = details.details_view(session, order_id, li_id)
        loose = next(e for e in d.elements if e.feed_key)
        retarget = session.query(StrategyTerms).filter_by(
            line_item_id=li_id, match_key="retargeting"
        ).one().id
    html = client.get(f"/orders/{order_id}/details/{li_id}").get_data(as_text=True)
    assert "Link to" in html

    client.post(
        f"/orders/{order_id}/details/{li_id}/link",
        data={"key": loose.feed_key, "term": str(retarget)},
    )
    with db_module.session_scope() as session:
        d = details.details_view(session, order_id, li_id)
        rows = {e.label: e for e in d.elements}
        assert not any(e.feed_key for e in d.elements)
        assert rows["D - Retargeting"].to_date == pytest.approx(3_000)
        assert rows["D - Retargeting"].linked

    client.post(
        f"/orders/{order_id}/details/{li_id}/link",
        data={"unlink": str(retarget), "key": loose.feed_key},
    )
    with db_module.session_scope() as session:
        d = details.details_view(session, order_id, li_id)
        assert any(e.feed_key for e in d.elements)


def test_media_budget_after_a_cpm_change():
    import details

    # 100K sold, 40K served for $80 at $2.00; CPM moves to $2.50.
    assert details.media_budget(80.0, 100_000, 40_000, 2.50) == pytest.approx(230.0)
    assert details.media_budget(80.0, 100_000, 40_000, None) is None


def test_cpm_flag_says_ahead_or_behind_on_the_cpm():
    import details

    assert details.cpm_flag(2.40, 2.00, 80, 100) == "Behind: CPM $2.40 vs $2.00"
    assert details.cpm_flag(1.60, 2.00, 120, 100) == "Ahead: CPM $1.60 vs $2.00"
    assert details.cpm_flag(2.40, 2.00, 100, 100) is None
    assert details.cpm_flag(1.60, 2.00, 80, 100) is None


def test_the_grid_shows_media_cost(site):
    import details
    import db as db_module

    app_module, (order_id, li_id) = site
    client = app_module.app.test_client()
    client.post(f"/orders/{order_id}/details/{li_id}/build")
    with db_module.session_scope() as session:
        d = details.details_view(session, order_id, li_id)
        behavioral = next(e for e in d.elements if e.label == "D - Behavioral")
    # 14 days x $20.40 served; 427.5K - 140K left at $2.04.
    assert behavioral.media_spent == pytest.approx(14 * 20.4)
    assert behavioral.media_budget == pytest.approx(14 * 20.4 + 287_500 * 2.04 / 1000)
    assert d.media_spent == pytest.approx(14 * 20.4)
    html = client.get(f"/orders/{order_id}/details/{li_id}").get_data(as_text=True)
    assert "Media Budg." in html


def test_the_morning_sweep_drafts_missing_splits(site):
    """The cron drafts what the Data page's button would, after a load."""
    import importlib.util
    import logging
    import pathlib

    import db as db_module
    from models import DailyDelivery, StrategyTerms

    _, (order_id, li_id) = site
    with db_module.session_scope() as session:
        session.query(DailyDelivery).delete()
    spec = importlib.util.spec_from_file_location(
        "ingest_script", pathlib.Path("scripts/ingest.py")
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    script.draft_splits(logging.getLogger("test"))
    script.draft_splits(logging.getLogger("test"))  # fills gaps only

    with db_module.session_scope() as session:
        terms = {
            t.match_key: t.monthly_target
            for t in session.query(StrategyTerms).filter_by(line_item_id=li_id)
        }
    assert terms == {
        "behavioral": pytest.approx(427_500),
        "retargeting": pytest.approx(22_500),
    }


def test_split_button_matches_what_is_serving(site):
    """Only Behavioral is serving, so the button's shares are 100 / 0."""
    import details
    import db as db_module

    app_module, (order_id, li_id) = site
    client = app_module.app.test_client()
    client.post(f"/orders/{order_id}/details/{li_id}/build")
    with db_module.session_scope() as session:
        d = details.details_view(session, order_id, li_id)
        by_label = {e.label: e.strategy_id for e in d.elements if e.sold}
        live = d.live_shares
    assert live[by_label["D - Behavioral"]] == pytest.approx(100)
    assert live[by_label["D - Retargeting"]] == pytest.approx(0)

    html = client.get(f"/orders/{order_id}/details/{li_id}").get_data(as_text=True)
    assert 'data-live="100.0"' in html
    assert "Match serving" in html


def test_serve_gap_shows_the_last_serve():
    import details

    as_of = dt.date(2026, 11, 6)
    never = details.GridRow(label="R")
    fresh = details.GridRow(label="B", by_date={dt.date(2026, 11, 5): 10.0})
    stale = details.GridRow(
        label="C", by_date={dt.date(2026, 11, 2): 10.0, dt.date(2026, 11, 4): 0.0}
    )
    assert never.serve_gap(as_of) == "n/a"
    assert fresh.serve_gap(as_of) is None
    assert stale.serve_gap(as_of) == "11/2"


def test_grid_names_the_dsp_lines_and_totals_the_rows(site):
    import details
    import db as db_module

    app_module, (order_id, li_id) = site
    client = app_module.app.test_client()
    client.post(f"/orders/{order_id}/details/{li_id}/build")
    with db_module.session_scope() as session:
        d = details.details_view(session, order_id, li_id)
        behavioral = next(e for e in d.elements if e.label == "D - Behavioral")
        assert behavioral.feed_names == ["Display - Behavioral"]
        # Each row's impressions left at its own CPM, added up.
        assert d.media_budget == pytest.approx(
            14 * 20.4 + (287_500 + 22_500) * 2.04 / 1000
        )
    html = client.get(f"/orders/{order_id}/details/{li_id}").get_data(as_text=True)
    assert "Display - Behavioral" in html
