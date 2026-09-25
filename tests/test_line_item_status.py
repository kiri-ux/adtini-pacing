"""Lines that have finished, and the tabs that keep them out of the way.

An order runs for as long as its longest line item. Most of what sits on an
open order finished months ago, and read together with the one line still
running they drag every total toward a flight nobody is buying.
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
    url = f"sqlite:///{tmp_path}/status.db"
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
        client = Client(name="Ram Jack")
        session.add(client)
        session.flush()
        order = Order(
            client_id=client.id, external_order_id="44807",
            name="Ram Jack #44807", pacing_type="impression",
            order_type="Insertion Order", status="IO Live",
            start_date=dt.date(2026, 3, 1), end_date=dt.date(2026, 12, 31),
        )
        session.add(order)
        session.flush()

        session.add_all([
            # Ran in the spring and finished.
            LineItem(
                order_id=order.id, external_id="101789", name="Display Ads",
                product="Display Ads", sort_order=0, status="IO Complete",
                start_date=dt.date(2026, 3, 1), end_date=dt.date(2026, 5, 31),
                monthly_impressions=175_000.0, total_impressions=525_000.0,
                goal_cpm=2.0,
            ),
            # Called off. Its dates say it is still in flight.
            LineItem(
                order_id=order.id, external_id="106571", name="Display Ads 2",
                product="Display Ads", sort_order=1, status="Cancelled",
                start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 12, 31),
                monthly_impressions=75_000.0, total_impressions=525_000.0,
                goal_cpm=2.0,
            ),
            # The one still running.
            LineItem(
                order_id=order.id, external_id="134625", name="Social Mirror Ads",
                product="Social Mirror Ads", sort_order=2, status="IO Live",
                start_date=dt.date(2026, 8, 1), end_date=dt.date(2026, 12, 31),
                monthly_impressions=66_666.0, total_impressions=333_330.0,
                goal_cpm=2.5,
            ),
        ])
        session.add(
            DailyDelivery(
                date=dt.date(2026, 9, 10), data_source="Social Mirror",
                campaign_id="CMP-9", strategy_id="S-1",
                client_name="Ram Jack", external_order_id="44807",
                external_line_item_id="134625", product="Social Mirror Ads",
                strategy_name="SM - Retargeting", strategy_type="Retargeting",
                impressions=2_000.0, clicks=6.0, cost=5.0,
            )
        )
        order_id = order.id

    app_module.app.config["TESTING"] = True
    return app_module, order_id


def _view(order_id):
    import db as db_module
    import views

    with db_module.session_scope() as session:
        return views.order_view(session, order_id)


def test_a_line_past_its_end_date_reads_as_ended(site):
    _, order_id = site
    by_name = {r.label: r for r in _view(order_id).rows}

    assert by_name["Display Ads"].state == "ended"
    assert by_name["Social Mirror Ads"].state == "running"


def test_a_cancelled_line_reads_as_cancelled_whatever_its_dates_say(site):
    """Its flight runs to December. It is not running."""
    _, order_id = site
    cancelled = {r.label: r for r in _view(order_id).rows}["Display Ads 2"]

    assert cancelled.end_date == dt.date(2026, 12, 31)
    assert cancelled.state == "cancelled"
    assert cancelled.is_open is False


def test_a_line_with_no_status_is_judged_on_its_dates_alone(site):
    """Most of the book predates the status column being read at all."""
    import db as db_module
    from models import LineItem

    _, order_id = site
    with db_module.session_scope() as session:
        for item in session.query(LineItem):
            item.status = None

    by_name = {r.label: r for r in _view(order_id).rows}
    assert by_name["Display Ads"].state == "ended"
    assert by_name["Display Ads 2"].state == "running"


def test_the_running_total_counts_only_the_running_lines(site):
    """A total that keeps counting finished lines is worse than no total."""
    _, order_id = site
    view = _view(order_id)

    assert [r.label for r in view.open_rows] == ["Social Mirror Ads"]
    assert len(view.closed_rows) == 2
    assert view.total.total_target == 525_000 + 525_000 + 333_330
    assert view.open_total.total_target == 333_330
    # And the other tab totals what the other tab shows.
    assert view.closed_total.total_target == 525_000 + 525_000


def test_the_page_opens_on_running_and_carries_both_totals(site):
    app_module, order_id = site
    body = app_module.app.test_client().get(f"/orders/{order_id}").get_data(
        as_text=True
    )

    assert 'class="elements show-open"' in body
    assert 'data-total="open"' in body and 'data-total="closed"' in body
    # Every row stays in the form, so Save still posts the finished ones.
    assert body.count('data-state="ended"') == 1
    assert body.count('data-state="cancelled"') == 1
    assert body.count('data-state="running"') == 1


def test_the_page_opens_on_ended_when_nothing_is_running(site):
    import db as db_module
    from models import LineItem

    app_module, order_id = site
    with db_module.session_scope() as session:
        for item in session.query(LineItem):
            item.status = "IO Complete"

    body = app_module.app.test_client().get(f"/orders/{order_id}").get_data(
        as_text=True
    )
    assert 'class="elements show-closed"' in body


def test_every_row_shows_its_line_item_id(site):
    app_module, order_id = site
    body = app_module.app.test_client().get(f"/orders/{order_id}").get_data(
        as_text=True
    )

    assert ">Item ID<" in body
    for external_id in ("101789", "106571", "134625"):
        assert f'<td class="idcell">{external_id}</td>' in body


def test_a_completed_line_is_ended_even_with_months_left_on_its_flight(site):
    """The buying team marked it complete. That beats the date."""
    import db as db_module
    from models import LineItem

    _, order_id = site
    with db_module.session_scope() as session:
        item = session.query(LineItem).filter_by(external_id="134625").one()
        item.status = "IO Complete"

    row = {r.label: r for r in _view(order_id).rows}["Social Mirror Ads"]
    assert row.end_date == dt.date(2026, 12, 31)
    assert row.state == "ended"


def test_a_line_with_no_goal_is_not_ended_just_for_having_none(site):
    """Where the flight is up to does not depend on anything being sold.
    Worked out only on the pacing path, every line with no goal set read as
    having no days left - which put all of them in the Ended tab on day
    one, including the only one actually running."""
    import db as db_module
    from models import LineItem

    _, order_id = site
    with db_module.session_scope() as session:
        item = session.query(LineItem).filter_by(external_id="134625").one()
        item.monthly_impressions = None
        item.total_impressions = None

    row = {r.label: r for r in _view(order_id).rows}["Social Mirror Ads"]
    assert row.needs_setup is True
    assert row.days_left > 0
    assert row.state == "running"


def test_a_spend_line_is_gridded_in_dollars_on_an_impressions_order(site):
    """The grid used the order's metric for every row, so a Performance Max
    day that spent $8.77 showed $293.00 - which was its impressions."""
    import datetime as dt

    import db as db_module
    from models import DailyDelivery, LineItem

    app_module, order_id = site
    with db_module.session_scope() as session:
        session.add(
            LineItem(
                order_id=order_id, external_id="121176",
                name="Performance Max Ads", product="Performance Max Ads",
                sort_order=3, status="IO Live", pacing_type="event",
                start_date=dt.date(2026, 8, 1), end_date=dt.date(2026, 12, 31),
                client_monthly_budget=1_750.0, client_total_budget=8_750.0,
            )
        )
        session.add(
            DailyDelivery(
                date=dt.date(2026, 9, 10),
                data_source="Google Ads Performance Max",
                campaign_id="CMP-P", strategy_id="P-1", client_name="Ram Jack",
                external_order_id="44807", external_line_item_id="121176",
                product="Performance Max Ads",
                strategy_name="Google Ads Combined order:", strategy_type="",
                impressions=293.0, clicks=9.0, cost=8.77, conversions=2.0,
            )
        )

    view = _view(order_id)
    pmax = next(r for r in view.rows if r.label == "Performance Max Ads")
    smirror = next(r for r in view.rows if r.label == "Social Mirror Ads")

    day = dt.date(2026, 9, 10)
    assert view.grid[pmax.line_item_id][day] == 8.77, "dollars, not impressions"
    assert view.grid[smirror.line_item_id][day] == 2_000.0, "impressions"


def test_a_months_goal_is_what_was_sold_for_that_month(site):
    """Two Display lines finished in May. Folding their targets into
    September's goal read the month as delivering 9% of a target it was
    never given."""
    import datetime as dt

    import db as db_module
    import views

    _, order_id = site
    with db_module.session_scope() as session:
        months = views.month_serve(views.order_view(session, order_id))

    sept = next(m for m in months if m.start == dt.date(2026, 9, 1))
    # Social Mirror only: the Display lines ran March-May and June-December,
    # and the June one is cancelled but its flight still covers September.
    assert sept.goal == 66_666 + 75_000
    assert 175_000 not in (sept.goal, sept.spend_goal), "the spring line is out"
