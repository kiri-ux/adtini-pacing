"""The details grid: one product line item, laid out the way the buying team
keeps it by hand on the Details & Updates tabs.

That tab is built by duplicating an old one and retyping it - run dates,
months, days, a date header per day, a row per strategy with its share of
the monthly goal and a starting CPM, then the monthly and to-date running
totals underneath - and filled in once a week by pasting a transposed
export. Everything on it is either sold (the orders file and the strategy
split), delivered (the feed) or arithmetic, so this builds it instead.

Two things a buyer still does by hand, and this page is where they do them:

* **the split** - how a product's monthly goal is divided across its
  targeting. `plan_split` drafts it from the setup guide's rules and the
  strategies the order says were bought; the buyer corrects it.
* **the weekly check** - the Updates sheet row. The figures are snapshotted
  from the pacing; the buyer answers the questions only they can.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select

import products
import ratecard
import sheets
import views
from models import DayNote, LineItem, StrategyTerms, WeeklyCheck
from pacing.calendar import inclusive_days, month_window, months_between
from pacing.engine import PacingRow


# The setup guide: retargeting falls outside the line item minimums, so it
# takes a fixed 10K a month off another strategy rather than an even share.
RETARGETING_MONTHLY_IMPRESSIONS = 10_000.0

# CTV + Video sells one budget across two rates. The guide puts two thirds
# of the impressions on the video half and a third on CTV.
MERGED_SPLIT = {"video": 2 / 3, "ctv": 1 / 3}

# adtini's Daily Pacing table: a day's serve reads red when it drops more
# than 40% under the daily goal or spikes more than 50% over it.
DAY_UNDER = 0.6
DAY_OVER = 1.5

# The guide's floor for display: a campaign should never sit below it.
DISPLAY_CTR_FLOOR = 0.0007


@dataclass
class GridRow:
    """One Campaign Elements row."""

    label: str
    monthly: float | None = None
    total: float | None = None
    daily: float | None = None
    rate: float | None = None
    daily_budget: float | None = None
    total_budget: float | None = None
    to_date: float = 0.0
    remaining: float | None = None
    on_pace: float | None = None
    pacing_pct: float | None = None
    by_date: dict[dt.date, float] = field(default_factory=dict)
    clicks_by_date: dict[dt.date, float] = field(default_factory=dict)
    impressions: float = 0.0
    clicks: float = 0.0
    # The StrategyTerms row behind it; None for targeting that is running
    # but sits in no sold row.
    strategy_id: int | None = None

    @property
    def sold(self) -> bool:
        return self.strategy_id is not None

    @property
    def ctr(self) -> float | None:
        return (self.clicks / self.impressions) if self.impressions else None


@dataclass
class DetailsView:
    view: "views.OrderView"
    item: LineItem
    row: PacingRow
    as_of: dt.date
    dates: list[dt.date]
    elements: list[GridRow]
    total_by_date: dict[dt.date, float]
    clicks_by_date: dict[dt.date, float]
    # Cumulative, flight to date and month to date, against even pacing.
    td_actual: dict[dt.date, float]
    td_target: dict[dt.date, float]
    month_actual: dict[dt.date, float]
    month_target: dict[dt.date, float]
    day_target: dict[dt.date, float]
    notes_by_date: dict[dt.date, list[DayNote]]
    checks: list[WeeklyCheck]
    # "under" / "over" on days outside adtini's daily pacing thresholds.
    day_state: dict[dt.date, str]
    flags: list[str]

    @property
    def order(self):
        return self.view.order

    @property
    def is_money(self) -> bool:
        return self.row.is_money

    @property
    def title(self) -> str:
        order = self.view.order
        number = f" #{order.external_order_id}" if order.external_order_id else ""
        return f"{self.view.client.name} - {self.item.product or self.item.name}{number}"

    @property
    def ext_id(self) -> str | None:
        ext = self.item.external_id
        return ext if ext and not ext.startswith("name:") else None

    @property
    def sold_names(self) -> list[str]:
        return views.sold_strategy_names(self.item)

    @property
    def months(self) -> int:
        if self.row.start_date and self.row.end_date:
            return months_between(self.row.start_date, self.row.end_date)
        return 0

    @property
    def has_split(self) -> bool:
        return any(e.sold for e in self.elements)

    @property
    def is_search(self) -> bool:
        return products._key(self.item.product or "") in {"payperclickads"}

    @property
    def last_check(self) -> WeeklyCheck | None:
        return self.checks[0] if self.checks else None

    @property
    def ctr_trend(self) -> str | None:
        """"up" or "down" since the last check, which is what the guide asks."""
        if not self.checks or self.checks[0].ctr is None or self.row.ctr is None:
            return None
        if self.row.ctr > self.checks[0].ctr:
            return "up"
        if self.row.ctr < self.checks[0].ctr:
            return "down"
        return "flat"

    def is_future(self, day: dt.date) -> bool:
        return day > self.as_of

    # The grid opens on one month: the one the data runs through, or the
    # nearest month of the flight. A column per day of a whole flight was
    # thousands of cells a row and buried the month being worked.
    show_all: bool = False
    month: dt.date | None = None  # first of the month to show, if picked

    @property
    def months_in_flight(self) -> list[dt.date]:
        return sorted({d.replace(day=1) for d in self.dates})

    @property
    def shown_month(self) -> dt.date | None:
        months = self.months_in_flight
        if not months:
            return None
        if self.month in months:
            return self.month
        current = self.as_of.replace(day=1)
        if current in months:
            return current
        return months[0] if current < months[0] else months[-1]

    @property
    def is_windowed(self) -> bool:
        return not self.show_all and len(self.months_in_flight) > 1

    def month_link(self, step: int) -> str | None:
        months = self.months_in_flight
        shown = self.shown_month
        if shown is None:
            return None
        i = months.index(shown) + step
        return months[i].strftime("%Y-%m") if 0 <= i < len(months) else None

    @property
    def shown_dates(self) -> list[dt.date]:
        if not self.is_windowed:
            return self.dates
        month = self.shown_month
        return [d for d in self.dates if d.replace(day=1) == month]


def details_view(
    session, order_id: int, line_item_id: int, as_of: dt.date | None = None
) -> DetailsView | None:
    view = views.order_view(
        session, order_id, as_of=as_of, line_item_id=line_item_id
    )
    if view is None:
        return None
    row = next((r for r in view.rows if r.line_item_id == line_item_id), None)
    if row is None or row.line_item is None:
        return None
    item: LineItem = row.line_item
    as_of = view.as_of
    money = row.is_money
    index = 2 if money else 0

    block = next(
        (b for b in views.strategy_blocks(session, view) if b.line_item.id == item.id),
        None,
    )
    grouped = views.group_by_targeting(item.product, view.strategies.get(item.id, []))
    ran = {key: series for key, _, series, _ in grouped}

    start, end = row.start_date, row.end_date
    in_flight = (
        (lambda d: start <= d <= end) if start and end else (lambda d: True)
    )

    def per_day(series, slot: int) -> dict[dt.date, float]:
        if series is None:
            return {}
        return {
            d: values[slot]
            for d, values in series.metrics_by_date.items()
            if in_flight(d) and values[slot]
        }

    elements: list[GridRow] = []
    claimed: set[str] = set()
    terms = {t.id: t for t in (block.terms if block else [])}
    for srow in block.rows if block else []:
        term = terms.get(srow.strategy_id)
        key = (sheets.match_key(term.label) if term else None) or srow.label.lower()
        if views.combined_strategy(item.product):
            key = views.COMBINED_KEY
        series = ran.get(key)
        if series is not None:
            claimed.add(key)
        sold = bool(srow.total_target)
        elements.append(
            GridRow(
                label=srow.label,
                monthly=srow.monthly_target or None,
                total=srow.total_target or None,
                daily=srow.daily_target if sold else None,
                rate=srow.unit_cost or None,
                daily_budget=srow.daily_budget if sold else None,
                total_budget=srow.total_budget if sold else None,
                to_date=srow.to_date,
                remaining=srow.remaining if sold else None,
                on_pace=srow.on_pace if sold else None,
                pacing_pct=srow.pacing_pct,
                by_date=per_day(series, index),
                clicks_by_date=per_day(series, 1),
                impressions=sum(per_day(series, 0).values()),
                clicks=sum(per_day(series, 1).values()),
                strategy_id=srow.strategy_id,
            )
        )
    for key, label, series, _ in grouped:
        if key in claimed:
            continue
        by_date = per_day(series, index)
        elements.append(
            GridRow(
                label=label,
                to_date=sum(by_date.values()),
                by_date=by_date,
                clicks_by_date=per_day(series, 1),
                impressions=sum(per_day(series, 0).values()),
                clicks=sum(per_day(series, 1).values()),
            )
        )

    # The whole flight, future days included, the way the tab is laid out:
    # the last date header is the end date, and the to-date target on it is
    # the sold total.
    dates: list[dt.date] = []
    if start and end:
        day = start
        while day <= end:
            dates.append(day)
            day += dt.timedelta(days=1)
    else:
        dates = sorted({d for e in elements for d in e.by_date})

    total_by_date: dict[dt.date, float] = {}
    clicks_by_date: dict[dt.date, float] = {}
    attr = "cost" if money else "impressions"
    for point in row.daily:
        if in_flight(point.date):
            total_by_date[point.date] = getattr(point, attr)
            clicks_by_date[point.date] = point.clicks

    td_actual: dict[dt.date, float] = {}
    td_target: dict[dt.date, float] = {}
    month_actual: dict[dt.date, float] = {}
    month_target: dict[dt.date, float] = {}
    day_target: dict[dt.date, float] = {}
    running = running_target = 0.0
    month_running = month_running_target = 0.0
    for day in dates:
        # The monthly rows start again on the 1st - the step the guide says
        # to check by hand ("make sure the first of the month has been
        # zeroed out").
        if day.day == 1:
            month_running = month_running_target = 0.0
        if start and end and row.monthly_target:
            window = month_window(day, start, end)
            days = inclusive_days(*window) if window else 0
            month_daily = row.monthly_target / days if days else 0.0
        else:
            month_daily = 0.0
        # The tab's own row: the sold total over the flight, one figure.
        day_target[day] = row.daily_target
        running_target += row.daily_target
        month_running_target += month_daily
        td_target[day] = running_target
        month_target[day] = month_running_target
        if day <= as_of:
            running += total_by_date.get(day, 0.0)
            month_running += total_by_date.get(day, 0.0)
            td_actual[day] = running
            month_actual[day] = month_running

    day_state: dict[dt.date, str] = {}
    for day, served in total_by_date.items():
        goal = day_target.get(day)
        if not goal:
            continue
        if served < goal * DAY_UNDER:
            day_state[day] = "under"
        elif served > goal * DAY_OVER:
            day_state[day] = "over"

    notes_by_date: dict[dt.date, list[DayNote]] = {}
    for note in views.day_log(session, order_id):
        if note.line_item_id in (None, item.id):
            notes_by_date.setdefault(note.date, []).append(note)

    checks = list(
        session.execute(
            select(WeeklyCheck)
            .where(WeeklyCheck.line_item_id == item.id)
            .order_by(WeeklyCheck.checked_on.desc(), WeeklyCheck.id.desc())
        ).scalars()
    )

    flags: list[str] = []
    if row.needs_setup:
        flags.append("No sold total or flight dates.")
    split_monthly = sum(e.monthly or 0 for e in elements)
    if elements and split_monthly and row.monthly_target and abs(
        split_monthly - row.monthly_target
    ) > 1:
        flags.append(
            f"Split totals {split_monthly:,.0f} a month. Sold: {row.monthly_target:,.0f}."
        )
    unsold = [e.label for e in elements if not e.sold and e.to_date]
    if unsold and any(e.sold for e in elements):
        flags.append("Not in split: " + ", ".join(unsold))
    if (
        not money
        and row.ctr is not None
        and products._key(item.product or "") in {"displayads", "nativedisplayads"}
        and row.ctr < DISPLAY_CTR_FLOOR
    ):
        flags.append(f"CTR {row.ctr * 100:.2f}% - under 0.07%")
    if checks and checks[0].tracking_conversions and not row.conversions:
        flags.append("Tracking conversions - none reported")

    return DetailsView(
        view=view,
        item=item,
        row=row,
        as_of=as_of,
        dates=dates,
        elements=elements,
        total_by_date=total_by_date,
        clicks_by_date=clicks_by_date,
        td_actual=td_actual,
        td_target=td_target,
        month_actual=month_actual,
        month_target=month_target,
        day_target=day_target,
        notes_by_date=notes_by_date,
        checks=checks,
        flags=flags,
        day_state=day_state,
    )


# --------------------------------------------------------------------------
# Building the split
# --------------------------------------------------------------------------
@dataclass
class PlannedStrategy:
    label: str
    monthly: float | None
    total: float | None
    rate: float | None


def _shares(names: list[str], monthly: float | None, money: bool) -> list[float]:
    """Each strategy's share of the monthly goal.

    Even, except retargeting on an impression line: it takes a flat 10K and
    the rest is split across the others. Alone, it takes everything.
    """
    retargeting = [
        i for i, name in enumerate(names) if sheets.match_key(name) == "retargeting"
    ]
    others = [i for i in range(len(names)) if i not in retargeting]
    if money or not monthly or not retargeting or not others:
        return [1.0 / len(names)] * len(names)
    fixed = min(RETARGETING_MONTHLY_IMPRESSIONS * len(retargeting), monthly / 2)
    each_fixed = fixed / len(retargeting) / monthly
    rest = (1.0 - each_fixed * len(retargeting)) / len(others)
    return [each_fixed if i in retargeting else rest for i in range(len(names))]


def _label(product: str | None, name: str) -> str:
    key = sheets.match_key(name) or name.lower()
    label = views.strategy_label(product, key, name)
    code = products.abbreviation(product)
    if code and not label.lower().startswith(code.lower()):
        label = f"{code} - {name}"
    return label


def plan_split(item: LineItem, money: bool) -> list[PlannedStrategy]:
    """Draft the split from what the order says was bought.

    Empty when the order lists no strategies; the caller then falls back to
    apportioning what is running.
    """
    names = views.sold_strategy_names(item)
    if not names:
        return []
    monthly = item.monthly_impressions or item.monthly_spend or item.client_monthly_budget
    total = item.total_impressions or item.total_spend or item.client_total_budget
    rate = item.goal_cpm or item.goal_cpc or item.goal_cpe
    scale = (total / monthly) if (total and monthly) else None

    halves = ratecard.merged_halves(item.product)
    plan: list[PlannedStrategy] = []
    if halves and not money:
        # One row per strategy per half. Retargeting only runs on the video
        # half - "one strategy plus retargeting".
        for half, portion in MERGED_SPLIT.items():
            product = {"video": "Video Ads", "ctv": "Connected TV Ads"}[half]
            wanted = [
                n for n in names
                if half == "video" or sheets.match_key(n) != "retargeting"
            ] or names
            half_monthly = (monthly * portion) if monthly else None
            for name, share in zip(wanted, _shares(wanted, half_monthly, money)):
                label = _label(product, name)
                part = (half_monthly * share) if half_monthly else None
                plan.append(
                    PlannedStrategy(
                        label=label,
                        monthly=part,
                        total=(part * scale) if (part and scale) else None,
                        rate=ratecard.merged_strategy_cpm(item.product, label) or rate,
                    )
                )
        return plan

    for name, share in zip(names, _shares(names, monthly, money)):
        part = (monthly * share) if monthly else None
        plan.append(
            PlannedStrategy(
                label=_label(item.product, name),
                monthly=part,
                total=(part * scale) if (part and scale) else (
                    (total * share) if total else None
                ),
                rate=rate,
            )
        )
    return plan


def build_split(session, item: LineItem, money: bool) -> tuple[int, str]:
    """Write a drafted split for a product that has none.

    Returns how many rows were added and where they came from. Never touches
    a product that already has rows - this fills a gap, it does not overwrite
    anybody's work.
    """
    existing = session.execute(
        select(func.count())
        .select_from(StrategyTerms)
        .where(StrategyTerms.line_item_id == item.id)
    ).scalar()
    if existing:
        return 0, "existing"

    plan = plan_split(item, money)
    if not plan:
        metric = "cost" if money else "impressions"
        running = views.strategy_series(session, [item], metric=metric).get(item.id, [])
        return views._draft_item(session, item, running), "delivery"

    count = session.execute(
        select(func.count())
        .select_from(StrategyTerms)
        .where(StrategyTerms.order_id == item.order_id)
    ).scalar() or 0
    seen: set[str] = set()
    for planned in plan:
        if planned.label in seen:
            continue
        seen.add(planned.label)
        session.add(
            StrategyTerms(
                order_id=item.order_id,
                line_item_id=item.id,
                label=planned.label,
                match_key=sheets.match_key(planned.label),
                monthly_target=planned.monthly,
                total_target=planned.total,
                rate=planned.rate,
                sort_order=count + len(seen) - 1,
                source="built from order",
                added_by_hand=True,
            )
        )
    return len(seen), "order"


def record_check(
    session, details: DetailsView, *, checked_on: dt.date, author: str | None,
    tracking_conversions: bool | None, search_terms_checked: bool,
    optimizations: str | None, notes: str | None,
) -> WeeklyCheck:
    """Save a weekly check, with the pacing as it stands."""
    row = details.row
    check = WeeklyCheck(
        order_id=details.order.id,
        line_item_id=details.item.id,
        checked_on=checked_on,
        data_through=details.as_of,
        author=author,
        ctr=row.ctr,
        to_date=row.to_date,
        on_pace=row.on_pace,
        pacing_pct=row.pacing_pct,
        month_to_date=row.month_to_date,
        month_on_pace=row.month_on_pace,
        month_pacing_pct=row.month_pacing_pct,
        conversions=row.conversions,
        tracking_conversions=tracking_conversions,
        search_terms_checked=search_terms_checked,
        optimizations=optimizations,
        notes=notes,
    )
    session.add(check)
    # An optimization lands in the grid's notes row on the day it was made,
    # which is where the hand-kept tab writes them.
    if optimizations:
        session.add(
            DayNote(
                order_id=details.order.id,
                line_item_id=details.item.id,
                date=checked_on,
                body=optimizations,
                author=author,
            )
        )
    return check
