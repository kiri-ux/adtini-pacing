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
import strategy_menu
import views
from models import DayNote, LineItem, StrategyTerms, WeeklyCheck
from pacing.calendar import inclusive_days, month_window, months_between
from pacing.engine import PacingRow


# Retargeting falls outside the line item minimums, so it takes a fixed 5%
# of the product's monthly impression goal rather than an even share.
RETARGETING_SHARE = 0.05

# CTV + Video sells one budget across two rates. The guide puts two thirds
# of the impressions on the video half and a third on CTV.
MERGED_SPLIT = {"video": 2 / 3, "ctv": 1 / 3}

# adtini's Daily Pacing table: a day's serve reads red when it drops more
# than 40% under the daily goal or spikes more than 50% over it.
DAY_UNDER = 0.6
DAY_OVER = 1.5

# The guide's floor for display: a campaign should never sit below it.
DISPLAY_CTR_FLOOR = 0.0007


# How each targeting reads in the strategy dropdown. Spelled out, so it is
# plain which part of the adtini form a row matches to.
OPTION_NAMES = {
    "behavioral": "Behavioral Targeting",
    "ai": "AI Targeting",
    "keyword": "Keyword Targeting",
    "category": "Category Targeting",
    "content": "Content Targeting",
    "lookalike": "Lookalike Audiences",
    "matching": "Audience Matching",
    "event": "Event Targeting",
    "b2b": "B2B Targeting",
}


# The running CPM's window: the last week up to the as-of date. Long enough
# that one thin day does not swing it, short enough that a bid change shows
# within days - the Forrest cap from $1.96 to $1.50 read in full by day 4.
RUN_CPM_DAYS = 7


def run_cpm(points, as_of: dt.date) -> float | None:
    """Cost per thousand over (date, impressions, cost) points in the window.

    None with no impressions in the window, rather than 0, because a line
    that did not serve did not clear at $0.
    """
    first = as_of - dt.timedelta(days=RUN_CPM_DAYS - 1)
    impressions = cost = 0.0
    for day, imps, spent in points:
        if first <= day <= as_of:
            impressions += imps or 0.0
            cost += spent or 0.0
    return (cost / impressions * 1000.0) if impressions else None


def _series_points(series):
    if series is None:
        return []
    return [(d, v[0], v[2]) for d, v in series.metrics_by_date.items()]


# How far the running CPM can sit from the setup CPM, and pacing from 100%,
# before a row says the CPM is why it is ahead or behind.
CPM_DRIFT = 0.05
# Days without a serve before a row shows its last serve date.
STALE_DAYS = 3
PACE_BAND = 0.05


def media_budget(spent: float, total: float | None, served: float,
                 cpm: float | None) -> float | None:
    """Media cost to set in the DSP: what has been spent, plus the
    impressions still owed at the current CPM. With the setup CPM unchanged
    and delivery on its rate this is total x CPM; after a CPM change it is
    the figure the budget has to move to."""
    if not total or not cpm:
        return None
    return spent + max(total - served, 0.0) * cpm / 1000.0


def cpm_flag(run: float | None, setup: float | None,
             to_date: float, on_pace: float | None) -> str | None:
    """Ahead or behind on impressions while the CPM has moved the same way."""
    if not run or not setup or not on_pace:
        return None
    pace = to_date / on_pace
    drift = run / setup - 1.0
    if pace < 1.0 - PACE_BAND and drift > CPM_DRIFT:
        return f"High CPM: ${run:.2f} vs ${setup:.2f}"
    if pace > 1.0 + PACE_BAND and drift < -CPM_DRIFT:
        return f"Low CPM: ${run:.2f} vs ${setup:.2f}"
    return None


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
    # Internal cost / impressions over the last RUN_CPM_DAYS served days up
    # to as_of. What the line is actually clearing at, beside the CPM it was
    # set up at - the card's starting and max sit on DetailsView.card.
    run_cpm: float | None = None
    # The StrategyTerms row behind it; None for targeting that is running
    # but sits in no sold row.
    strategy_id: int | None = None
    # What the strategy targets - categories, keywords, places - one per
    # line. Typed in by hand until the adtini forms carry it.
    targets: str | None = None
    # Feed cost in the flight, and the media budget at the row's CPM.
    media_spent: float | None = None
    media_budget: float | None = None
    cpm_flag: str | None = None
    # The order lists this targeting for the product.
    ordered: bool = False
    # Running and on no row: the delivery group it is, for linking by hand.
    feed_key: str | None = None
    # Delivery linked to this row by hand: (group key, label).
    linked: list[tuple[str, str]] = field(default_factory=list)
    # The DSP strategy line names delivering to this row.
    feed_names: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return any(self.by_date.values())

    @property
    def last_serve(self) -> dt.date | None:
        served = [d for d, v in self.by_date.items() if v]
        return max(served) if served else None

    def serve_gap(self, as_of: dt.date) -> str | None:
        """"n/a" when it never served, the last date when that was
        STALE_DAYS or more before as_of, else None."""
        last = self.last_serve
        if last is None:
            return "n/a"
        if (as_of - last).days >= STALE_DAYS:
            return f"{last.month}/{last.day}"
        return None

    @property
    def target_list(self) -> list[str]:
        return [t.strip() for t in (self.targets or "").splitlines() if t.strip()]

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
    # What the product needs to serve tomorrow to land its goal; the Day
    # Budg. column on the Total row.
    day_budget: float | None = None
    # The whole product's running CPM, same window as GridRow.run_cpm.
    run_cpm: float | None = None
    media_spent: float | None = None
    media_budget: float | None = None
    # The rate card entry for the product: starting / max CPM to read the
    # running CPM against. None for products bought on spend.
    card: "ratecard.Rate | None" = None

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
    def live_shares(self) -> dict[int, float]:
        """Each sold row's share of what the product served over the last
        RUN_CPM_DAYS days to as_of, in percent. Empty when nothing served.

        What the split button sets the % column to: the mix actually running,
        not the drafted one.
        """
        first = self.as_of - dt.timedelta(days=RUN_CPM_DAYS - 1)
        served = {
            e.strategy_id: sum(
                v for d, v in e.by_date.items() if first <= d <= self.as_of
            )
            for e in self.elements
            if e.sold
        }
        total = sum(served.values())
        if not total:
            return {}
        return {sid: v / total * 100 for sid, v in served.items()}

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

    @property
    def strategy_options(self) -> list[str]:
        """The strategies a row can be: this product's code, then targeting."""
        code = products.abbreviation(self.item.product)
        names = strategy_menu.options(self.item.product) or list(dict.fromkeys(
            OPTION_NAMES.get(k, v) for k, v in sheets.TARGETING_LABELS.items()
        ))
        return [f"{code} - {name}" if code else name for name in names]

    def selected_option(self, label: str) -> str | None:
        """The option a stored label is: by its wording, else by what it
        targets, taking the plainest name ("Behavioral Targeting" over "B2B
        Behavioral Targeting") when several target the same thing."""
        opts = self.strategy_options
        short = self.short_label(label).strip().lower()
        for o in opts:
            if self.short_label(o).lower() == short:
                return o
        key = sheets.match_key(label)
        if key is None:
            return None
        same = [o for o in opts if sheets.match_key(o) == key]
        return min(same, key=len) if same else None

    @property
    def product_pill(self) -> dict | None:
        """The product's code and colors, for the pill left of the grid."""
        code = products.abbreviation(self.item.product)
        if not code:
            return None
        entry = products.lookup(self.item.product)
        return {
            "code": code,
            "hex": entry.hex if entry else "#123A63",
            "text_hex": entry.text_hex if entry else "#FFFFFF",
        }

    def short_label(self, label: str) -> str:
        """A strategy label without the product code the pill already shows."""
        code = products.abbreviation(self.item.product)
        prefix = f"{code} - " if code else ""
        return label[len(prefix):] if prefix and label.startswith(prefix) else label

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
    labels = {key: label for key, label, _, _ in grouped}

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

    def catch_up(monthly, total, by_date, day: dt.date) -> float | None:
        """What a day has to serve so the goal still lands.

        The goal left over the days left, recalculated each day: a line that
        started late, or ran short yesterday, needs more than the even share
        to make it up. Against the month's goal where there is one, else the
        flight's. Days after today are all asked the same - what tomorrow
        needs - because nothing has served on them yet.
        """
        if not (start and end):
            return None
        anchor = min(day, as_of + dt.timedelta(days=1))
        if monthly:
            window = month_window(day, start, end)
            if not window:
                return None
            first, last = window
            goal = monthly
        elif total:
            first, last, goal = start, end, total
        else:
            return None
        anchor = max(anchor, first)
        left = inclusive_days(anchor, last)
        if left <= 0:
            return None
        served = sum(v for d, v in by_date.items() if first <= d < anchor)
        return max(goal - served, 0.0) / left

    tomorrow = as_of + dt.timedelta(days=1)

    def day_budget(goal: float | None, rate: float | None) -> float | None:
        if goal is None:
            return None
        if money:
            return goal
        return goal * rate / 1000.0 if rate else None

    # The targeting the order says was bought. A strategy that was bought
    # but has not started serving (retargeting waiting on a pixel, say) has
    # no delivery to show it by, so it is listed from here.
    ordered: dict[str, str] = {}
    for name in views.sold_strategy_names(item):
        ordered.setdefault(sheets.match_key(name) or name.lower(), name)
    combined = bool(views.combined_strategy(item.product))

    elements: list[GridRow] = []
    claimed: set[str] = set()
    split_keys: set[str] = set()
    terms = {t.id: t for t in (block.terms if block else [])}
    for srow in block.rows if block else []:
        term = terms.get(srow.strategy_id)
        if combined:
            key = views.COMBINED_KEY
            series = ran.get(key)
            keys = [key] if series is not None else []
        elif term is not None:
            series, keys = views.claim(term, ran)
            key = views.term_keys(term)[0]
        else:
            key = srow.label.lower()
            series = ran.get(key)
            keys = [key] if series is not None else []
        claimed.update(keys)
        split_keys.add(key)
        linked = [
            (k, labels[k]) for k in (views.term_keys(term)[1:] if term else [])
            if k in ran
        ]
        sold = bool(srow.total_target)
        spent = None if money else sum(per_day(series, 2).values())
        row_run_cpm = None if money else run_cpm(_series_points(series), as_of)
        elements.append(
            GridRow(
                label=srow.label,
                monthly=srow.monthly_target or None,
                total=srow.total_target or None,
                daily=srow.daily_target if sold else None,
                rate=srow.unit_cost or None,
                daily_budget=day_budget(
                    catch_up(srow.monthly_target, srow.total_target,
                             per_day(series, index), tomorrow),
                    srow.unit_cost,
                ) if sold else None,
                total_budget=srow.total_budget if sold else None,
                to_date=srow.to_date,
                remaining=srow.remaining if sold else None,
                on_pace=srow.on_pace if sold else None,
                pacing_pct=srow.pacing_pct,
                by_date=per_day(series, index),
                clicks_by_date=per_day(series, 1),
                impressions=sum(per_day(series, 0).values()),
                clicks=sum(per_day(series, 1).values()),
                run_cpm=row_run_cpm,
                media_spent=spent,
                media_budget=None if money else media_budget(
                    spent, srow.total_target, srow.to_date, srow.unit_cost),
                cpm_flag=None if money else cpm_flag(
                    row_run_cpm, srow.unit_cost, srow.to_date, srow.on_pace),
                strategy_id=srow.strategy_id,
                targets=term.targets if term else None,
                ordered=combined or key in ordered,
                linked=linked,
                feed_names=sorted(ran[key].names) if key in ran else [],
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
                run_cpm=None if money else run_cpm(_series_points(series), as_of),
                ordered=combined or key in ordered,
                feed_key=key,
                feed_names=sorted(series.names),
            )
        )
    if not combined:
        for key, name in ordered.items():
            if key in split_keys or key in ran:
                continue
            elements.append(
                GridRow(
                    label=_label(item.product, name),
                    ordered=True,
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

    product_spent = sum(p.cost for p in row.daily if in_flight(p.date))
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
        # What each day needed, given what had served before it.
        need = catch_up(row.monthly_target, row.total_target, total_by_date, day)
        day_target[day] = need if need is not None else row.daily_target
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
        # adtini's own rule: against the even share, total over days.
        goal = row.daily_target
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

    # With a split, the Total row's budgets are the rows' added up, each at
    # its own CPM. The product's goal CPM is the client's rate, not what any
    # DSP line is set to, so budgets from it never matched the rows above.
    priced = [e for e in elements if e.sold and e.rate]
    split_day_budget = (
        sum(e.daily_budget or 0.0 for e in priced) if priced else None
    )
    split_left = None
    if priced and not money:
        split_left = sum(
            max((e.total or 0.0) - e.to_date, 0.0) * e.rate / 1000.0 for e in priced
        )

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
        day_budget=split_day_budget if split_day_budget is not None else day_budget(
            catch_up(row.monthly_target, row.total_target, total_by_date, tomorrow),
            row.unit_cost,
        ),
        notes_by_date=notes_by_date,
        checks=checks,
        flags=flags,
        day_state=day_state,
        run_cpm=None if money else run_cpm(
            [(p.date, p.impressions, p.cost) for p in row.daily], as_of),
        media_spent=None if money else product_spent,
        media_budget=None if money else (
            product_spent + split_left if split_left is not None else media_budget(
                product_spent, row.total_target, row.to_date, row.unit_cost)
        ),
        card=None if money else ratecard.lookup(
            item.product, restricted=bool(item.restricted)),
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


def _shares(
    names: list[str], monthly: float | None, money: bool,
    goal: float | None = None,
) -> list[float]:
    """Each strategy's share of `monthly`.

    Even, except retargeting on an impression line: it takes 5% of `goal`
    (the product's whole monthly goal, which is more than `monthly` on one
    half of CTV + Video) and the rest is split across the others. Alone, it
    takes everything.
    """
    retargeting = [
        i for i, name in enumerate(names) if sheets.match_key(name) == "retargeting"
    ]
    others = [i for i in range(len(names)) if i not in retargeting]
    if money or not monthly or not retargeting or not others:
        return [1.0 / len(names)] * len(names)
    fixed = min(RETARGETING_SHARE * (goal or monthly) * len(retargeting), monthly / 2)
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
            for name, share in zip(wanted, _shares(wanted, half_monthly, money, monthly)):
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

    # The order's strategies decide the rows: what was bought, running yet or
    # not (retargeting waiting on a pixel still gets its row). Only a product
    # whose order lists no strategies is drafted from what is running.
    plan = plan_split(item, money)
    if not plan:
        metric = "cost" if money else "impressions"
        running = views.strategy_series(session, [item], metric=metric).get(item.id, [])
        return views._draft_item(session, item, running), "delivery"

    return write_plan(session, item, plan), "order"


def write_plan(session, item: LineItem, plan: list[PlannedStrategy]) -> int:
    """Add a drafted split's rows. Returns how many were added."""
    count = session.execute(
        select(func.count())
        .select_from(StrategyTerms)
        .where(StrategyTerms.order_id == item.order_id)
    ).scalar() or 0
    # Rows added earlier in the same run are not flushed yet.
    count += sum(
        1 for pending in session.new
        if isinstance(pending, StrategyTerms) and pending.order_id == item.order_id
    )
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
    return len(seen)


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
