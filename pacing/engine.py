"""Pacing math for the three sheet layouts.

Every type answers the same question - "is this ahead of or behind where it
should be by now?" - but on a different primary metric:

* impression pacing paces on delivered impressions (most orders)
* click pacing paces on spend (PPC, LinkedIn)
* event pacing paces on spend (Performance Max)

Sign convention matches the buying team's sheet: a *negative* pacing percent
means over-delivering, positive means under-delivering.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field, asdict
from typing import Iterable, Sequence

from models import (
    PACING_CLICK,
    PACING_EVENT,
    PACING_IMPRESSION,
    LineItem,
    Order,
)
from pacing.calendar import (
    elapsed_days,
    inclusive_days,
    month_bounds,
    month_window,
)


# Retail on Performance Max is four times the internal cost. Orders that
# carry both figures get their own ratio; the rest take this, which is what
# they would be priced at anyway.
DEFAULT_CLIENT_COST_RATIO = 4.0


@dataclass
class DailyPoint:
    date: dt.date
    impressions: float = 0.0
    clicks: float = 0.0
    cost: float = 0.0
    conversions: float = 0.0


@dataclass
class PacingRow:
    """One "Campaign Elements" row, computed."""

    line_item_id: int | None
    label: str
    pacing_type: str
    start_date: dt.date | None
    end_date: dt.date | None
    flight_days: int = 0

    # Sold / target side.
    monthly_target: float = 0.0
    total_target: float = 0.0
    daily_target: float = 0.0
    unit_cost: float = 0.0           # CPM, CPC or CPE depending on type
    daily_budget: float = 0.0
    total_budget: float = 0.0

    # Delivered side, life of flight.
    to_date: float = 0.0
    remaining: float = 0.0
    on_pace: float = 0.0
    pacing_delta: float = 0.0
    pacing_pct: float | None = None

    # Delivered side, current month.
    month_days: int = 0
    month_daily_target: float = 0.0
    month_to_date: float = 0.0
    month_on_pace: float = 0.0
    month_pacing_delta: float = 0.0
    month_pacing_pct: float | None = None

    # Event pacing carries the client's budget beside Google's spend, and an
    # event count beside it. Not paced on, but part of the row.
    client_monthly_budget: float = 0.0
    client_total_budget: float = 0.0
    google_monthly_spend: float = 0.0
    google_total_spend: float = 0.0
    # Platform cost -> client cost. Derived per order where the figures are
    # there, and 4x where they are not, which is what retail runs at.
    client_cost_ratio: float = DEFAULT_CLIENT_COST_RATIO
    monthly_events: float = 0.0
    total_events: float = 0.0

    # Secondary metrics, carried for display.
    impressions: float = 0.0
    clicks: float = 0.0
    cost: float = 0.0
    conversions: float = 0.0
    ctr: float | None = None
    effective_unit_cost: float | None = None

    # From the rate card: what this product is expected to do, and what the
    # supply partner charges for it.
    goal_metric: str | None = None      # "ctr" or "vr"
    goal_value: float | None = None
    goal_label: str | None = None
    partner_hard_cost: float | None = None
    margin: float | None = None
    goal_cpm_source: str | None = None

    # Where the flight is up to, for the daily-rate columns.
    days_elapsed: int = 0
    days_left: int = 0

    needs_setup: bool = False
    daily: list[DailyPoint] = field(default_factory=list)
    # The row's sold terms, so a template can read fields the engine does not
    # promote (client budgets and event counts on the Performance Max sheet).
    line_item: object | None = None

    # --- the figures the pacing table reads ---------------------------
    #
    # Two different percentages, and they answer different questions.
    # `goal_ratio` is how much of what was sold has run, which is what the
    # bar fills to. `delivery_ratio` is how that compares with where it
    # should be by now, which is the number beside it - 100% is on pace,
    # under is behind, over is ahead.

    @property
    def goal_ratio(self) -> float | None:
        if not self.total_target:
            return None
        return self.to_date / self.total_target

    @property
    def month_goal_ratio(self) -> float | None:
        if not self.monthly_target:
            return None
        return self.month_to_date / self.monthly_target

    @property
    def expected_ratio(self) -> float | None:
        """Where the bar's marker sits - what should have run by now."""
        if not self.total_target:
            return None
        return self.on_pace / self.total_target

    @property
    def month_expected_ratio(self) -> float | None:
        if not self.monthly_target:
            return None
        return self.month_on_pace / self.monthly_target

    @property
    def delivery_ratio(self) -> float | None:
        if not self.on_pace:
            return None
        return self.to_date / self.on_pace

    @property
    def month_delivery_ratio(self) -> float | None:
        if not self.month_on_pace:
            return None
        return self.month_to_date / self.month_on_pace

    @property
    def avg_daily(self) -> float | None:
        """What it has actually been serving a day."""
        if not self.days_elapsed:
            return None
        return self.to_date / self.days_elapsed

    @property
    def daily_needed(self) -> float | None:
        """What it has to serve a day from here to finish on goal."""
        if not self.days_left:
            return None
        return max(self.remaining, 0.0) / self.days_left

    @property
    def metric_label(self) -> str:
        return {
            PACING_IMPRESSION: "Impr.",
            PACING_CLICK: "Spend",
            PACING_EVENT: "Spend",
        }[self.pacing_type]

    @property
    def is_money(self) -> bool:
        return self.pacing_type in (PACING_CLICK, PACING_EVENT)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("daily", "line_item")}
        d["metric_label"] = self.metric_label
        d["is_money"] = self.is_money
        return d


def _pct(on_pace: float, to_date: float) -> float | None:
    """Pacing percent, using the sheet's own formula.

    (on_pace - to_date) / on_pace. Negative is ahead of pace. Undefined until
    the flight has started, which the sheet shows as #DIV/0!.
    """
    if not on_pace:
        return None
    return (on_pace - to_date) / on_pace


def _sum(points: Iterable[DailyPoint], attr: str) -> float:
    return float(sum(getattr(p, attr) for p in points))


class Totals:
    """The four metrics, summed."""

    __slots__ = ("impressions", "clicks", "cost", "conversions")

    def __init__(self) -> None:
        self.impressions = self.clicks = self.cost = self.conversions = 0.0

    def add(self, point: DailyPoint) -> None:
        self.impressions += point.impressions
        self.clicks += point.clicks
        self.cost += point.cost
        self.conversions += point.conversions

    def of(self, attr: str) -> float:
        return float(getattr(self, attr))


def _bucket(
    points: Sequence[DailyPoint],
    flight: tuple[dt.date, dt.date] | None,
    month: tuple[dt.date, dt.date] | None,
) -> tuple[Totals, Totals, Totals]:
    """Everything, the flight, and the month - in one pass over the days.

    Summed separately per metric and per window, this walked the same days
    nine times. On the overview that is every line item on the book, and it
    was most of the page's wait.
    """
    every, in_flight, in_month = Totals(), Totals(), Totals()
    for point in points:
        every.add(point)
        if flight and flight[0] <= point.date <= flight[1]:
            in_flight.add(point)
            if month and month[0] <= point.date <= month[1]:
                in_month.add(point)
        elif not flight and month and month[0] <= point.date <= month[1]:
            in_month.add(point)
    return every, in_flight, in_month


def _primary_attr(pacing_type: str) -> str:
    return "impressions" if pacing_type == PACING_IMPRESSION else "cost"


@dataclass
class DeliveryTotals:
    """Delivery already summed into the three windows a row needs.

    The overview paces every line item on the book, and fetching each one's
    days to add them up here meant pulling a third of a million rows to show
    a hundred and fifty. Where the database can sum them instead, it hands
    the answer over in this shape and the row skips the bucketing entirely.
    """

    every: Totals
    in_flight: Totals
    in_month: Totals


def compute_row(
    line_item: LineItem,
    order: Order,
    daily: Sequence[DailyPoint],
    as_of: dt.date,
    totals: DeliveryTotals | None = None,
) -> PacingRow:
    """Compute one pacing row from its sold terms and its delivery.

    `totals` is the same sums worked out in the database. When it is given,
    `daily` is not read - the caller has nothing to show day by day.
    """
    pacing_type = order.pacing_type or PACING_IMPRESSION
    start = line_item.start_date or order.start_date
    end = line_item.end_date or order.end_date

    row = PacingRow(
        line_item_id=line_item.id,
        label=line_item.name,
        pacing_type=pacing_type,
        start_date=start,
        end_date=end,
        line_item=line_item,
    )

    if pacing_type == PACING_IMPRESSION:
        row.monthly_target = line_item.monthly_impressions or 0.0
        row.total_target = line_item.total_impressions or 0.0
        row.unit_cost = line_item.goal_cpm or 0.0
    elif pacing_type == PACING_CLICK:
        row.monthly_target = line_item.monthly_spend or 0.0
        row.total_target = line_item.total_spend or 0.0
        row.unit_cost = line_item.goal_cpc or 0.0
    else:
        # Performance Max paces what the client is billed against what the
        # client is charged - not the platform spend, which is the ad spend
        # net of the management fee and would read as permanently under.
        row.monthly_target = line_item.client_monthly_budget or 0.0
        row.total_target = line_item.client_total_budget or 0.0
        row.unit_cost = line_item.goal_cpe or 0.0
        row.client_monthly_budget = line_item.client_monthly_budget or 0.0
        row.client_total_budget = line_item.client_total_budget or 0.0
        row.google_total_spend = line_item.google_total_spend or 0.0
        row.google_monthly_spend = line_item.google_monthly_spend or 0.0
        row.monthly_events = line_item.monthly_events or 0.0
        row.total_events = line_item.total_events or 0.0
        # The feed reports platform cost. What the client is charged is that
        # grossed up by the same ratio the order sold it at.
        if line_item.google_total_spend and line_item.client_total_budget:
            row.client_cost_ratio = (
                line_item.client_total_budget / line_item.google_total_spend
            )

    points = sorted(daily, key=lambda p: p.date) if totals is None else []
    row.daily = points
    attr = _primary_attr(pacing_type)
    gross = row.client_cost_ratio if pacing_type == PACING_EVENT else 1.0

    paceable = bool(start and end and row.total_target)
    flight = (start, end) if paceable else None
    window = month_window(as_of, start, end) if paceable else None
    if window:
        month_bounds_used = (window[0], min(as_of, window[1]))
    else:
        month_bounds_used = month_bounds(as_of)

    if totals is None:
        every, in_flight, in_month = _bucket(points, flight, month_bounds_used)
    else:
        every, in_flight, in_month = (
            totals.every, totals.in_flight, totals.in_month
        )
    counted = in_flight if paceable else every

    row.impressions = counted.impressions
    row.clicks = counted.clicks
    row.cost = counted.cost
    row.conversions = counted.conversions
    row.ctr = (row.clicks / row.impressions) if row.impressions else None
    row.to_date = counted.of(attr) * gross

    # Without dates or a sold total there is nothing to pace against. Delivery
    # still shows, flagged so the order book can be filled in.
    if not paceable:
        row.needs_setup = True
        row.remaining = row.total_target - row.to_date
        row.month_to_date = in_month.of(attr) * gross
        _attach_effective_cost(row)
        _attach_rate_card(row, line_item)
        return row

    row.flight_days = inclusive_days(start, end)
    row.daily_target = row.total_target / row.flight_days if row.flight_days else 0.0

    if pacing_type == PACING_IMPRESSION:
        row.daily_budget = row.daily_target * row.unit_cost / 1000.0
        row.total_budget = row.total_target * row.unit_cost / 1000.0
    else:
        # Targets are already money; the budget columns mirror them.
        row.daily_budget = row.daily_target
        row.total_budget = row.total_target

    row.remaining = row.total_target - row.to_date
    row.days_elapsed = elapsed_days(as_of, start, end)
    row.days_left = max(inclusive_days(start, end) - row.days_elapsed, 0)
    row.on_pace = row.daily_target * row.days_elapsed
    row.pacing_delta = row.to_date - row.on_pace
    row.pacing_pct = _pct(row.on_pace, row.to_date)

    if window:
        w_start, w_end = window
        row.month_days = inclusive_days(w_start, w_end)
        # The month's sold amount spread over the days the flight actually
        # covers in that month, not a flat 1/30th.
        row.month_daily_target = (
            row.monthly_target / row.month_days if row.month_days else 0.0
        )
        row.month_to_date = in_month.of(attr) * gross
        row.month_on_pace = row.month_daily_target * elapsed_days(as_of, w_start, w_end)
        row.month_pacing_delta = row.month_to_date - row.month_on_pace
        row.month_pacing_pct = _pct(row.month_on_pace, row.month_to_date)

    _attach_effective_cost(row)
    _attach_rate_card(row, line_item)
    return row


def _attach_rate_card(row: PacingRow, line_item: LineItem) -> None:
    """Hang the product's expected performance and margin off the row."""
    import ratecard

    row.goal_cpm_source = line_item.goal_cpm_source
    rate = ratecard.lookup(line_item.product, restricted=bool(line_item.restricted))
    if rate is None:
        return
    row.goal_metric = rate.goal_metric
    row.goal_value = rate.goal_value
    row.goal_label = rate.goal_label
    row.partner_hard_cost = rate.partner_hard_cost
    row.margin = rate.margin_at(row.unit_cost)


def _attach_effective_cost(row: PacingRow) -> None:
    """Delivered cost per unit, for comparison against the goal."""
    if row.pacing_type == PACING_IMPRESSION:
        row.effective_unit_cost = (
            row.cost / row.impressions * 1000.0 if row.impressions else None
        )
    elif row.pacing_type == PACING_CLICK:
        row.effective_unit_cost = row.cost / row.clicks if row.clicks else None
    else:
        row.effective_unit_cost = row.cost / row.conversions if row.conversions else None


def total_row(rows: Sequence[PacingRow], pacing_type: str) -> PacingRow:
    """The sheet's `Total:` row - sums, with the percentages recomputed."""
    total = PacingRow(
        line_item_id=None,
        label="Total",
        pacing_type=pacing_type,
        start_date=min((r.start_date for r in rows if r.start_date), default=None),
        end_date=max((r.end_date for r in rows if r.end_date), default=None),
    )
    if not rows:
        return total

    for attr in (
        "monthly_target", "total_target", "daily_target", "daily_budget",
        "total_budget", "to_date", "remaining", "on_pace", "month_daily_target",
        "month_to_date", "month_on_pace", "impressions", "clicks", "cost",
        "conversions", "client_monthly_budget", "client_total_budget",
        "google_monthly_spend", "google_total_spend", "monthly_events",
        "total_events",
    ):
        setattr(total, attr, sum(getattr(r, attr) for r in rows))

    total.flight_days = max((r.flight_days for r in rows), default=0)
    total.month_days = max((r.month_days for r in rows), default=0)
    total.days_elapsed = max((r.days_elapsed for r in rows), default=0)
    total.days_left = max((r.days_left for r in rows), default=0)
    total.pacing_delta = total.to_date - total.on_pace
    total.pacing_pct = _pct(total.on_pace, total.to_date)
    total.month_pacing_delta = total.month_to_date - total.month_on_pace
    total.month_pacing_pct = _pct(total.month_on_pace, total.month_to_date)
    total.ctr = (total.clicks / total.impressions) if total.impressions else None
    total.needs_setup = any(r.needs_setup for r in rows)

    # A blended goal rate, weighted by what was sold.
    if total.pacing_type == PACING_IMPRESSION and total.total_target:
        total.unit_cost = total.total_budget / total.total_target * 1000.0
    elif rows:
        priced = [r for r in rows if r.unit_cost]
        total.unit_cost = sum(r.unit_cost for r in priced) / len(priced) if priced else 0.0

    _attach_effective_cost(total)

    # A goal only means something on the total when every row shares it.
    labels = {r.goal_label for r in rows if r.goal_label}
    if len(labels) == 1:
        only = rows[0 if rows[0].goal_label else -1]
        for candidate in rows:
            if candidate.goal_label:
                only = candidate
                break
        total.goal_metric = only.goal_metric
        total.goal_value = only.goal_value
        total.goal_label = only.goal_label

    costed = [r for r in rows if r.partner_hard_cost and r.total_target]
    if costed:
        weight = sum(r.total_target for r in costed)
        total.partner_hard_cost = (
            sum(r.partner_hard_cost * r.total_target for r in costed) / weight
        )
        if total.partner_hard_cost and total.unit_cost:
            total.margin = (
                (total.partner_hard_cost - total.unit_cost) / total.partner_hard_cost
            )

    sources = {r.goal_cpm_source for r in rows if r.goal_cpm_source}
    total.goal_cpm_source = sources.pop() if len(sources) == 1 else None

    # The order's own platform-to-client ratio, from its summed figures, so
    # the page can say what the delivered side has been turned into.
    if total.google_total_spend and total.client_total_budget:
        total.client_cost_ratio = total.client_total_budget / total.google_total_spend

    return total


def health(pacing_pct: float | None, tolerance: float = 0.10) -> str:
    """Bucket a pacing percent for display.

    Inside +/- tolerance is on pace. Beyond it, under or over.
    """
    # NaN is not a pacing percent. Left alone it bucketed as "over", because
    # every comparison against a NaN is false and the last branch won - so a
    # row with no usable number was coloured as if it were a real problem.
    if pacing_pct is None or math.isnan(pacing_pct):
        return "unknown"
    if abs(pacing_pct) <= tolerance:
        return "on-pace"
    return "under" if pacing_pct > 0 else "over"
