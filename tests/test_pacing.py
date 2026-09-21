"""The pacing math, checked against rows read off the buying team's sheets."""
from __future__ import annotations

import datetime as dt

import pytest

from models import LineItem, Order
from pacing.calendar import elapsed_days, inclusive_days, month_window
from pacing.engine import DailyPoint, compute_row, health, total_row


def days(start, end, per_day, metric="impressions"):
    out, day = [], start
    while day <= end:
        out.append(DailyPoint(date=day, **{metric: per_day}))
        day += dt.timedelta(days=1)
    return out


# --- calendar --------------------------------------------------------------
def test_inclusive_days_counts_both_ends():
    assert inclusive_days(dt.date(2026, 7, 1), dt.date(2026, 9, 30)) == 92


def test_elapsed_days_caps_at_flight_length():
    start, end = dt.date(2026, 7, 1), dt.date(2026, 7, 10)
    assert elapsed_days(dt.date(2026, 6, 30), start, end) == 0
    assert elapsed_days(dt.date(2026, 7, 1), start, end) == 1
    assert elapsed_days(dt.date(2026, 12, 1), start, end) == 10


def test_month_window_clips_to_the_flight():
    # A flight starting mid-month only has the rest of that month to deliver in.
    window = month_window(dt.date(2026, 9, 20), dt.date(2026, 9, 17), dt.date(2027, 2, 28))
    assert window == (dt.date(2026, 9, 17), dt.date(2026, 9, 30))


def test_month_window_is_none_outside_the_flight():
    assert month_window(dt.date(2026, 11, 1), dt.date(2026, 9, 1), dt.date(2026, 9, 30)) is None


# --- impression pacing -----------------------------------------------------
@pytest.fixture
def comfort_inn():
    """Comfort Inn - Somerset, read off the impression tab.

    120,000 over 7/1-9/30 at a $5.00 CPM, which the sheet works out to
    1,304.35 a day, $6.52 a day and $600.00 total.
    """
    order = Order(
        id=1, client_id=1, name="Comfort Inn - Somerset", pacing_type="impression",
        start_date=dt.date(2026, 7, 1), end_date=dt.date(2026, 9, 30),
    )
    item = LineItem(
        id=1, order_id=1, name="FB - Category", monthly_impressions=40_000,
        total_impressions=120_000, goal_cpm=5.00,
    )
    return order, item


def test_impression_targets_match_the_sheet(comfort_inn):
    order, item = comfort_inn
    row = compute_row(item, order, [], dt.date(2026, 7, 1))
    assert row.flight_days == 92
    assert round(row.daily_target, 2) == 1304.35
    assert round(row.daily_budget, 2) == 6.52
    assert round(row.total_budget, 2) == 600.00


def test_on_pace_is_the_daily_target_times_days_run(comfort_inn):
    order, item = comfort_inn
    # 15 days in, the sheet's On Pace column reads 19,565.
    row = compute_row(item, order, [], dt.date(2026, 7, 15))
    assert round(row.on_pace) == 19_565


def test_pacing_percent_is_negative_when_over_delivering(comfort_inn):
    order, item = comfort_inn
    delivered = days(dt.date(2026, 7, 1), dt.date(2026, 7, 15), 2_000)
    row = compute_row(item, order, delivered, dt.date(2026, 7, 15))
    assert row.to_date == 30_000
    assert row.pacing_delta > 0          # ahead of plan
    assert row.pacing_pct < 0            # which the sheet shows as negative
    assert health(row.pacing_pct) == "over"


def test_pacing_percent_is_positive_when_under_delivering(comfort_inn):
    order, item = comfort_inn
    delivered = days(dt.date(2026, 7, 1), dt.date(2026, 7, 15), 500)
    row = compute_row(item, order, delivered, dt.date(2026, 7, 15))
    assert row.pacing_pct > 0
    assert health(row.pacing_pct) == "under"


def test_delivery_outside_the_flight_is_not_counted(comfort_inn):
    order, item = comfort_inn
    before = days(dt.date(2026, 6, 20), dt.date(2026, 6, 30), 5_000)
    inside = days(dt.date(2026, 7, 1), dt.date(2026, 7, 5), 1_000)
    row = compute_row(item, order, before + inside, dt.date(2026, 7, 5))
    assert row.to_date == 5_000


def test_month_target_spreads_over_the_days_the_flight_covers():
    """A flight starting on 9/17 owes its September impressions in 14 days."""
    order = Order(
        id=2, client_id=1, name="Blair County WIC", pacing_type="impression",
        start_date=dt.date(2026, 9, 17), end_date=dt.date(2027, 2, 28),
    )
    item = LineItem(
        id=2, order_id=2, name="D - Behavioral", monthly_impressions=200_000,
        total_impressions=720_000, goal_cpm=4.00,
    )
    row = compute_row(item, order, [], dt.date(2026, 9, 21))
    assert row.month_days == 14
    assert round(row.month_daily_target, 2) == round(200_000 / 14, 2)
    # Five days in (the 17th through the 21st).
    assert round(row.month_on_pace) == round(200_000 / 14 * 5)


def test_a_row_without_sold_terms_is_flagged_but_still_reports_delivery():
    order = Order(id=3, client_id=1, name="New order", pacing_type="impression")
    item = LineItem(id=3, order_id=3, name="D - AI")
    row = compute_row(item, order, days(dt.date(2026, 9, 1), dt.date(2026, 9, 3), 1_000),
                      dt.date(2026, 9, 3))
    assert row.needs_setup
    assert row.to_date == 3_000
    assert row.pacing_pct is None
    assert health(row.pacing_pct) == "unknown"


# --- click pacing ----------------------------------------------------------
def test_click_pacing_paces_on_spend_not_clicks():
    """Bud's Auto #49440, read off the click tab: $23,814 over 8/4-1/31."""
    order = Order(
        id=4, client_id=1, name="Bud's Auto #49440", pacing_type="click",
        start_date=dt.date(2026, 8, 4), end_date=dt.date(2027, 1, 31),
    )
    item = LineItem(
        id=4, order_id=4, name="PPC - Keywords", monthly_spend=3_969.0,
        total_spend=23_814.0, goal_cpc=0.78,
    )
    delivered = [
        DailyPoint(date=dt.date(2026, 8, 4) + dt.timedelta(days=i),
                   cost=743.875, clicks=950, impressions=2_000)
        for i in range(8)
    ]
    row = compute_row(item, order, delivered, dt.date(2026, 8, 11))

    assert row.flight_days == 181
    assert round(row.daily_target, 2) == 131.57     # $23,814 / 181
    assert round(row.to_date, 2) == 5_951.00        # TD Spend
    assert round(row.remaining, 2) == 17_863.00     # Spend Left
    assert round(row.on_pace, 0) == 1_053           # 131.57 x 8 days
    assert round(row.pacing_pct * 100, 0) == -465   # the sheet's -465.38%


def test_click_pacing_reports_effective_cpc():
    order = Order(id=5, client_id=1, name="PPC", pacing_type="click",
                  start_date=dt.date(2026, 9, 1), end_date=dt.date(2026, 9, 30))
    item = LineItem(id=5, order_id=5, name="PPC - Keywords", monthly_spend=1_000,
                    total_spend=1_000, goal_cpc=0.80)
    delivered = [DailyPoint(date=dt.date(2026, 9, 1), cost=100.0, clicks=200.0)]
    row = compute_row(item, order, delivered, dt.date(2026, 9, 1))
    assert row.effective_unit_cost == 0.50


# --- event pacing ----------------------------------------------------------
def test_event_pacing_paces_on_google_spend():
    order = Order(id=6, client_id=1, name="PMax", pacing_type="event",
                  start_date=dt.date(2025, 12, 31), end_date=dt.date(2026, 2, 9))
    item = LineItem(
        id=6, order_id=6, name="PMax", client_monthly_budget=2_250.0,
        client_total_budget=2_250.0, google_monthly_spend=562.50,
        google_total_spend=562.50, goal_cpe=2.20, monthly_events=400, total_events=400,
    )
    row = compute_row(item, order, [], dt.date(2025, 12, 31))
    assert row.flight_days == 41
    assert round(row.daily_target, 2) == 13.72      # $562.50 / 41
    assert row.client_total_budget == 2_250.0
    assert row.monthly_events == 400


def test_event_pacing_effective_cost_is_per_conversion():
    order = Order(id=7, client_id=1, name="PMax", pacing_type="event",
                  start_date=dt.date(2026, 9, 1), end_date=dt.date(2026, 9, 30))
    item = LineItem(id=7, order_id=7, name="PMax", google_monthly_spend=300,
                    google_total_spend=300, goal_cpe=2.00)
    delivered = [DailyPoint(date=dt.date(2026, 9, 1), cost=100.0, conversions=50.0)]
    row = compute_row(item, order, delivered, dt.date(2026, 9, 1))
    assert row.effective_unit_cost == 2.0


# --- totals ----------------------------------------------------------------
def test_total_sums_rows_and_recomputes_the_percent(comfort_inn):
    order, item = comfort_inn
    second = LineItem(id=8, order_id=1, name="D - Behavioral",
                      monthly_impressions=20_000, total_impressions=60_000, goal_cpm=3.00)
    as_of = dt.date(2026, 7, 15)
    rows = [
        compute_row(item, order, days(dt.date(2026, 7, 1), as_of, 1_000), as_of),
        compute_row(second, order, days(dt.date(2026, 7, 1), as_of, 400), as_of),
    ]
    total = total_row(rows, "impression")

    assert total.total_target == 180_000
    assert total.to_date == 15_000 + 6_000
    assert round(total.on_pace) == round(rows[0].on_pace + rows[1].on_pace)
    # A blended CPM, weighted by what was sold, not a flat average.
    assert round(total.unit_cost, 4) == round((600.0 + 180.0) / 180_000 * 1000, 4)


def test_total_of_nothing_is_empty_not_an_error():
    total = total_row([], "impression")
    assert total.to_date == 0
    assert total.pacing_pct is None


def test_health_tolerance():
    assert health(0.05) == "on-pace"
    assert health(-0.05) == "on-pace"
    assert health(0.4) == "under"
    assert health(-0.4) == "over"
    assert health(None) == "unknown"
