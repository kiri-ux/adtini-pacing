"""Summing delivery in the database instead of fetching every day of it.

The overview shows no daily figures, so adding them up in Python meant
pulling a third of a million rows to render a page of a hundred and fifty
orders. The database sums them in one row per line item instead.

The shortcut only holds while everything a line item ran falls inside its
flight, because the flight differs per line item and the database cannot
window it in the same pass. Delivery outside the flight has to go back to
the slow path, and these check that it does - a row that quietly counted
over-delivery from after its end date would read as on pace when it is not.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import views
from models import Base, Client, DailyDelivery, LineItem, Order
from pacing.engine import compute_row

AS_OF = dt.date(2026, 9, 20)


def _build(session, *, flight_end: dt.date, delivery_days: list[dt.date]):
    client = Client(name="Acme")
    session.add(client)
    session.flush()
    order = Order(
        client_id=client.id,
        external_order_id="900",
        name="Acme #900",
        pacing_type="impression",
        order_type="Insertion Order",
        status="IO Live",
        start_date=dt.date(2026, 8, 1),
        end_date=flight_end,
    )
    session.add(order)
    session.flush()
    item = LineItem(
        order_id=order.id,
        external_id="7001",
        name="Display Ads",
        product="Display Ads",
        monthly_impressions=30_000.0,
        total_impressions=90_000.0,
        goal_cpm=2.5,
    )
    session.add(item)
    session.flush()
    for day in delivery_days:
        session.add(
            DailyDelivery(
                date=day,
                data_source="Display",
                campaign_id="CMP-1",
                strategy_id=f"S-{day}",
                client_name="Acme",
                external_order_id="900",
                external_line_item_id="7001",
                product="Display Ads",
                impressions=1_000.0,
                clicks=10.0,
                cost=2.5,
                conversions=1.0,
            )
        )
    session.flush()
    return order, item


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as handle:
        yield handle


def _both_ways(session, order, item):
    """The summed row and the day-by-day row, for comparison."""
    totals, spilled = views._totals_by_line_item(session, [item], AS_OF)
    daily = views._daily_by_line_item(session, [item])
    slow = compute_row(item, order, daily.get(item.id, []), AS_OF)
    fast = compute_row(
        item, order, daily.get(item.id, []), AS_OF, totals=totals.get(item.id)
    )
    return slow, fast, totals, spilled


def test_delivery_inside_the_flight_takes_the_fast_path(session):
    days = [dt.date(2026, 8, 1) + dt.timedelta(days=n) for n in range(40)]
    order, item = _build(session, flight_end=dt.date(2026, 10, 31), delivery_days=days)

    slow, fast, totals, spilled = _both_ways(session, order, item)

    assert spilled == [], "nothing ran outside the flight"
    assert item.id in totals
    assert (fast.to_date, fast.month_to_date) == (slow.to_date, slow.month_to_date)
    assert (fast.impressions, fast.clicks, fast.cost) == (
        slow.impressions, slow.clicks, slow.cost
    )
    assert fast.pacing_pct == slow.pacing_pct


def test_delivery_past_the_end_date_goes_back_to_the_slow_path(session):
    """The case the shortcut cannot answer, and must not guess at.

    A campaign that kept running after its end date would otherwise have
    that over-delivery counted as if it were sold, reading as on pace.
    """
    days = [dt.date(2026, 8, 1) + dt.timedelta(days=n) for n in range(40)]
    order, item = _build(session, flight_end=dt.date(2026, 8, 20), delivery_days=days)

    slow, fast, totals, spilled = _both_ways(session, order, item)

    assert [s.id for s in spilled] == [item.id]
    assert item.id not in totals, "it must not be answered from the summed path"
    # 20 days inside the flight, not the 40 that ran.
    assert slow.to_date == 20_000
    assert slow.to_date < 40_000


def test_the_two_paths_agree_on_the_whole_overview(session):
    """Whatever route a row takes, the page reads the same."""
    days = [dt.date(2026, 8, 1) + dt.timedelta(days=n) for n in range(40)]
    _build(session, flight_end=dt.date(2026, 10, 31), delivery_days=days)

    rows = views.overview(session, as_of=AS_OF)
    assert len(rows) == 1
    assert rows[0].total.to_date == 40_000
    assert rows[0].total.impressions == 40_000
