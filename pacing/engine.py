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
from dataclasses import dataclass, field, asdict
from typing import Iterable, Sequence

from models import (
    PACING_CLICK,
    PACING_EVENT,
    PACING_IMPRESSION,
    LineItem,
    Order,
)
from pacing.calendar import elapsed_days, inclusive_days, month_window


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
    monthly_events: float = 0.0
    total_events: float = 0.0

    # Secondary metrics, carried for display.
    impressions: float = 0.0
    clicks: float = 0.0
    cost: float = 0.0
    conversions: float = 0.0
    ctr: float | None = None
    effective_unit_cost: float | None = None

    needs_setup: bool = False
    daily: list[DailyPoint] = field(default_factory=list)
    # The row's sold terms, so a template can read fields the engine does not
    # promote (client budgets and event counts on the Performance Max sheet).
    line_item: object | None = None

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


def _primary_attr(pacing_type: str) -> str:
    return "impressions" if pacing_type == PACING_IMPRESSION else "cost"


def compute_row(
    line_item: LineItem,
    order: Order,
    daily: Sequence[DailyPoint],
    as_of: dt.date,
) -> PacingRow:
    """Compute one pacing row from its sold terms and its delivery."""
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
        row.monthly_target = line_item.google_monthly_spend or 0.0
        row.total_target = line_item.google_total_spend or 0.0
        row.unit_cost = line_item.goal_cpe or 0.0
        row.client_monthly_budget = line_item.client_monthly_budget or 0.0
        row.client_total_budget = line_item.client_total_budget or 0.0
        row.monthly_events = line_item.monthly_events or 0.0
        row.total_events = line_item.total_events or 0.0

    points = sorted(daily, key=lambda p: p.date)
    row.daily = points
    row.impressions = _sum(points, "impressions")
    row.clicks = _sum(points, "clicks")
    row.cost = _sum(points, "cost")
    row.conversions = _sum(points, "conversions")
    row.ctr = (row.clicks / row.impressions) if row.impressions else None

    attr = _primary_attr(pacing_type)
    row.to_date = _sum(points, attr)

    # Without dates or a sold total there is nothing to pace against. Delivery
    # still shows, flagged so the order book can be filled in.
    if not start or not end or not row.total_target:
        row.needs_setup = True
        row.remaining = row.total_target - row.to_date
        row.month_to_date = _sum(
            [p for p in points if p.date.year == as_of.year and p.date.month == as_of.month],
            attr,
        )
        _attach_effective_cost(row)
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

    flight_points = [p for p in points if start <= p.date <= end]
    row.to_date = _sum(flight_points, attr)
    row.impressions = _sum(flight_points, "impressions")
    row.clicks = _sum(flight_points, "clicks")
    row.cost = _sum(flight_points, "cost")
    row.conversions = _sum(flight_points, "conversions")
    row.ctr = (row.clicks / row.impressions) if row.impressions else None

    row.remaining = row.total_target - row.to_date
    row.on_pace = row.daily_target * elapsed_days(as_of, start, end)
    row.pacing_delta = row.to_date - row.on_pace
    row.pacing_pct = _pct(row.on_pace, row.to_date)

    window = month_window(as_of, start, end)
    if window:
        w_start, w_end = window
        row.month_days = inclusive_days(w_start, w_end)
        # The month's sold amount spread over the days the flight actually
        # covers in that month, not a flat 1/30th.
        row.month_daily_target = (
            row.monthly_target / row.month_days if row.month_days else 0.0
        )
        row.month_to_date = _sum(
            [p for p in flight_points if w_start <= p.date <= min(as_of, w_end)], attr
        )
        row.month_on_pace = row.month_daily_target * elapsed_days(as_of, w_start, w_end)
        row.month_pacing_delta = row.month_to_date - row.month_on_pace
        row.month_pacing_pct = _pct(row.month_on_pace, row.month_to_date)

    _attach_effective_cost(row)
    return row


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
        "monthly_events", "total_events",
    ):
        setattr(total, attr, sum(getattr(r, attr) for r in rows))

    total.flight_days = max((r.flight_days for r in rows), default=0)
    total.month_days = max((r.month_days for r in rows), default=0)
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
    return total


def health(pacing_pct: float | None, tolerance: float = 0.10) -> str:
    """Bucket a pacing percent for display.

    Inside +/- tolerance is on pace. Beyond it, under or over.
    """
    if pacing_pct is None:
        return "unknown"
    if abs(pacing_pct) <= tolerance:
        return "on-pace"
    return "under" if pacing_pct > 0 else "over"
