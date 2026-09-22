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
def test_event_pacing_targets_the_client_budget():
    """Performance Max is paced on what the client is billed.

    It used to target the platform spend, which is that budget net of the
    management fee - so the order read as under by the size of the fee no
    matter how it was running.
    """
    order = Order(id=6, client_id=1, name="PMax", pacing_type="event",
                  start_date=dt.date(2025, 12, 31), end_date=dt.date(2026, 2, 9))
    item = LineItem(
        id=6, order_id=6, name="PMax", client_monthly_budget=2_250.0,
        client_total_budget=2_250.0, google_monthly_spend=562.50,
        google_total_spend=562.50, goal_cpe=2.20, monthly_events=400, total_events=400,
    )
    row = compute_row(item, order, [], dt.date(2025, 12, 31))
    assert row.flight_days == 41
    assert row.total_target == 2_250.0
    assert round(row.daily_target, 2) == 54.88      # $2,250 / 41
    assert row.google_total_spend == 562.50         # kept, for the ratio
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


# --- rate card -------------------------------------------------------------
def test_pacing_uses_the_setup_cpm_not_the_retail_one():
    """Three CPMs exist per line item and only one is the pacing rate.

    The orders file's budget over its impressions is the retail rate the
    client is billed. Pacing on it would show a budget the buying team never
    bought at, so the rate card's setup rate wins wherever it has one.
    """
    from orderbook import resolve_goal_cpm

    # Display is on the card at $2.50; the retail rate here works out to $8.
    cpm, source = resolve_goal_cpm("Display", False, total_budget=8_000,
                                   total_impressions=1_000_000)
    assert cpm == 2.50
    assert source == "rate card"


def test_a_product_the_card_does_not_price_falls_back_to_the_orders_file():
    from orderbook import resolve_goal_cpm

    cpm, source = resolve_goal_cpm("Some New Product", False, total_budget=8_000,
                                   total_impressions=1_000_000)
    assert cpm == 8.0
    assert source == "orders file"


def test_products_bought_on_spend_have_no_cpm_at_all():
    """PPC, LinkedIn and PMax are bought on budget, not on a rate."""
    from orderbook import resolve_goal_cpm

    assert resolve_goal_cpm("PPC", False, None, None) == (None, None)
    assert resolve_goal_cpm("LinkedIn", False, None, None) == (None, None)


def test_restricted_categories_take_their_own_higher_rate():
    import ratecard

    assert ratecard.setup_cpm("Display") == 2.50
    assert ratecard.setup_cpm("Display", restricted=True) == 4.00


def test_the_card_matches_the_hand_kept_sheet():
    """A Display line on the buying team's sheet reads $2.50, which is the
    card's Max - not its $1.00 Starting."""
    import ratecard

    assert ratecard.setup_cpm("Display") == 2.50
    assert ratecard.setup_cpm("CTV") == 14.00
    assert ratecard.setup_cpm("Meta") == 7.00


def test_margin_is_measured_against_the_partner_hard_cost():
    """The sheet's own "Margin v Max" column: Display is 37.50%."""
    import ratecard

    rate = ratecard.lookup("Display")
    assert rate.partner_hard_cost == 4.00
    assert round(rate.margin_at(2.50) * 100, 2) == 37.50


def test_performance_goals_parse_off_the_card():
    import ratecard

    display = ratecard.lookup("Display")
    assert display.goal_metric == "ctr"
    assert display.goal_value == 0.004

    ctv = ratecard.lookup("CTV")
    assert ctv.goal_metric == "vr"
    assert ctv.goal_value == 0.90


def test_the_row_carries_its_goal_and_margin():
    order = Order(id=90, client_id=1, name="Acme", pacing_type="impression",
                  start_date=dt.date(2026, 9, 1), end_date=dt.date(2026, 9, 30))
    item = LineItem(id=90, order_id=90, name="D - Behavioral", product="Display",
                    monthly_impressions=100_000, total_impressions=100_000,
                    goal_cpm=2.50, goal_cpm_source="rate card")
    row = compute_row(item, order, [], dt.date(2026, 9, 1))
    assert row.goal_label == "0.40% CTR"
    assert round(row.margin * 100, 2) == 37.50
    assert row.goal_cpm_source == "rate card"


# --- what spend products pace on -------------------------------------------
def test_ppc_paces_on_ad_spend_not_the_client_budget():
    """The client's budget carries the management fee, which never reaches
    the platform - pacing on it would read as permanently under."""
    from orderbook import _apply_sold_terms

    item = LineItem(id=200, order_id=200, name="PPC", product="PPC Ads")
    _apply_sold_terms(item, {
        "product": "PPC Ads",
        "total_ppc_spend": 23_814.0, "monthly_ppc_spend": 3_969.0,
        "total_campaign_budget": 40_000.0, "monthly_budget": 6_666.0,
    }, "click")
    assert item.total_spend == 23_814.0
    assert item.monthly_spend == 3_969.0


def test_linkedin_paces_on_its_own_ad_spend():
    from orderbook import _apply_sold_terms

    item = LineItem(id=201, order_id=201, name="LI", product="LinkedIn Ads")
    _apply_sold_terms(item, {
        "product": "LinkedIn Ads",
        "total_linkedin_spend": 9_000.0, "monthly_linkedin_spend": 1_500.0,
        "total_ppc_spend": 111.0, "monthly_budget": 2_000.0,
    }, "click")
    assert item.total_spend == 9_000.0
    assert item.monthly_spend == 1_500.0


def test_performance_max_paces_the_client_budget_against_client_cost():
    """Target is what the client is billed; the feed reports platform cost,
    so it is grossed up by the ratio the order was sold at."""
    order = Order(id=202, client_id=1, name="PMax", pacing_type="event",
                  start_date=dt.date(2026, 9, 1), end_date=dt.date(2026, 9, 30))
    item = LineItem(
        id=202, order_id=202, name="PMax", product="Performance Max Ads",
        client_monthly_budget=2_250.0, client_total_budget=2_250.0,
        google_monthly_spend=562.50, google_total_spend=562.50,
    )
    # A day at the platform's $562.50 is a full month of the client's budget.
    delivered = [DailyPoint(date=dt.date(2026, 9, 1), cost=562.50)]
    row = compute_row(item, order, delivered, dt.date(2026, 9, 1))

    assert row.total_target == 2_250.0          # the client's budget, not Google's
    assert row.client_cost_ratio == 4.0         # 2,250 / 562.50
    assert row.to_date == 2_250.0               # platform cost grossed up


def test_event_pacing_without_a_platform_figure_uses_the_retail_multiple():
    """Retail is four times internal, so an order that carries no platform
    figure is still grossed up rather than compared against a cost the
    client was never billed."""
    order = Order(id=203, client_id=1, name="PMax", pacing_type="event",
                  start_date=dt.date(2026, 9, 1), end_date=dt.date(2026, 9, 30))
    item = LineItem(id=203, order_id=203, name="PMax",
                    client_monthly_budget=900.0, client_total_budget=900.0)
    row = compute_row(item, order, [DailyPoint(date=dt.date(2026, 9, 1), cost=30.0)],
                      dt.date(2026, 9, 1))
    assert row.client_cost_ratio == 4.0
    assert row.to_date == 120.0


# --- the figures the pacing table reads ------------------------------------
def test_the_table_separates_how_much_has_run_from_whether_it_is_on_pace():
    """A bar at 60% is early or late depending on the date, so the table
    carries both: the fill is progress toward goal, the number beside it is
    progress against where it should be."""
    order = Order(id=300, client_id=1, name="Acme", pacing_type="impression",
                  start_date=dt.date(2026, 9, 1), end_date=dt.date(2026, 9, 30))
    item = LineItem(id=300, order_id=300, name="D", monthly_impressions=140_000,
                    total_impressions=140_000, goal_cpm=2.5)
    row = compute_row(item, order, days(dt.date(2026, 9, 1), dt.date(2026, 9, 20), 5_000),
                      dt.date(2026, 9, 20))

    assert row.to_date == 100_000
    assert round(row.goal_ratio, 4) == round(100_000 / 140_000, 4)      # the fill
    assert round(row.expected_ratio, 4) == round(20 / 30, 4)            # the tick
    assert round(row.delivery_ratio, 4) == round(100_000 / 93_333.33, 4)  # the number
    assert row.delivery_ratio > 1                                       # ahead


def test_daily_serve_against_what_is_needed_from_here():
    order = Order(id=301, client_id=1, name="Acme", pacing_type="impression",
                  start_date=dt.date(2026, 9, 1), end_date=dt.date(2026, 9, 30))
    item = LineItem(id=301, order_id=301, name="D", monthly_impressions=140_000,
                    total_impressions=140_000, goal_cpm=2.5)
    row = compute_row(item, order, days(dt.date(2026, 9, 1), dt.date(2026, 9, 20), 5_000),
                      dt.date(2026, 9, 20))

    assert row.days_elapsed == 20
    assert row.days_left == 10
    assert row.avg_daily == 5_000                    # what it has been doing
    assert row.daily_needed == 4_000                 # 40,000 left over 10 days


def test_a_finished_flight_needs_nothing_more_a_day():
    order = Order(id=302, client_id=1, name="Acme", pacing_type="impression",
                  start_date=dt.date(2026, 9, 1), end_date=dt.date(2026, 9, 30))
    item = LineItem(id=302, order_id=302, name="D", monthly_impressions=100,
                    total_impressions=100, goal_cpm=2.5)
    row = compute_row(item, order, [], dt.date(2026, 10, 15))
    assert row.days_left == 0
    assert row.daily_needed is None


def test_over_delivery_never_asks_for_a_negative_daily_rate():
    order = Order(id=303, client_id=1, name="Acme", pacing_type="impression",
                  start_date=dt.date(2026, 9, 1), end_date=dt.date(2026, 9, 30))
    item = LineItem(id=303, order_id=303, name="D", monthly_impressions=10_000,
                    total_impressions=10_000, goal_cpm=2.5)
    row = compute_row(item, order, days(dt.date(2026, 9, 1), dt.date(2026, 9, 20), 5_000),
                      dt.date(2026, 9, 20))
    assert row.remaining < 0
    assert row.daily_needed == 0.0
