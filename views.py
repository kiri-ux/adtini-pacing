"""Read models for the two things the buying team looks at.

* `order_view` - one order's pacing rows plus its daily grid. The per-order
  sheet, one section.
* `overview` - one line per order across every client. The summary tab.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import case, false, func, or_, select, tuple_
from sqlalchemy.orm import selectinload

from models import (
    CampaignLink,
    Client,
    DailyDelivery,
    DayNote,
    LineItem,
    Order,
    StrategyTerms,
)
import ratecard
import products
import sheets
from orderbook import PACEABLE_ORDER_TYPE, line_item_label
from pacing.calendar import inclusive_days, month_bounds, month_window
from pacing.engine import (
    DailyPoint,
    DeliveryTotals,
    PacingRow,
    Totals,
    compute_row,
    health,
    total_row,
)


def latest_delivery_date(session) -> dt.date | None:
    """The last day the feed covers - everything is paced as of this date."""
    return session.execute(select(func.max(DailyDelivery.date))).scalar()


def earliest_delivery_date(session) -> dt.date | None:
    """The first day the feed covers.

    Each drop is a rolling 31-day window, so on day one the tool only knows
    about the last month. A flight that started before this date has a
    life-of-flight figure that is short by whatever ran earlier, and the
    pages say so rather than quietly under-reporting. Running the sweep daily
    closes the gap as history accumulates.
    """
    return session.execute(select(func.min(DailyDelivery.date))).scalar()


@dataclass
class _Join:
    """How a day of delivery finds the line item it ran under.

    Two routes, tried in that order:

    * **by id** - the two exports share `order_id` and `line_item_id`, which
      covers most of the book. Delivery carrying no order id was adopted
      under a line item keyed on its own line item id alone; that is the
      second entry in `by_id`.
    * **by link** - a campaign the buying team attached to a line item by
      hand, for the campaigns the ids never matched on.

    The id route wins where both apply, so a campaign that starts matching on
    its own after a rebuild cannot be counted twice.
    """

    by_id: dict[tuple[str | None, str | None], int]
    by_link: dict[tuple[str, str], int]
    order_ids: set[str | None]

    def __bool__(self) -> bool:
        return bool(self.by_id or self.by_link)

    @property
    def external_ids(self) -> list[str | None]:
        return [li_id for (_, li_id) in self.by_id]

    def where(self):
        """Everything either route could claim, as one filter.

        The links go in as a row-value `IN` rather than an `OR` per link: the
        overview builds one join across every order on the page, and a
        thousand `OR`s is a query plan no index can help.
        """
        clauses = []
        if self.by_id:
            clauses.append(DailyDelivery.external_line_item_id.in_(self.external_ids))
        if self.by_link:
            clauses.append(
                tuple_(DailyDelivery.data_source, DailyDelivery.campaign_id).in_(
                    list(self.by_link)
                )
            )
        return or_(*clauses) if clauses else false()

    def resolve(
        self,
        order_id: str | None,
        line_item_id: str | None,
        data_source: str | None = None,
        campaign_id: str | None = None,
    ) -> int | None:
        target = self.by_id.get((order_id, line_item_id))
        if target is None and order_id not in self.order_ids:
            # Adopted delivery, keyed on its line item id alone.
            target = self.by_id.get((None, line_item_id))
        if target is None and data_source is not None and campaign_id is not None:
            target = self.by_link.get((data_source, campaign_id))
        return target


def _delivery_join(session, line_items: list[LineItem]) -> _Join:
    by_id = {
        (li.order.external_order_id, li.external_id): li.id
        for li in line_items
        if li.external_id
    }
    ids = [li.id for li in line_items]
    by_link: dict[tuple[str, str], int] = {}
    if ids:
        rows = session.execute(
            select(
                CampaignLink.data_source,
                CampaignLink.campaign_id,
                CampaignLink.line_item_id,
            ).where(CampaignLink.line_item_id.in_(ids))
        )
        by_link = {(source, campaign): li_id for source, campaign, li_id in rows}
    return _Join(
        by_id=by_id,
        by_link=by_link,
        order_ids={li.order.external_order_id for li in line_items},
    )


def _daily_from_strategies(
    line_items: list[LineItem], strategies: dict[int, list["StrategySeries"]]
) -> dict[int, list[DailyPoint]]:
    """A line item's days, added up from the strategies that ran them.

    The two used to be separate aggregates over the same rows, which on the
    order page meant reading the whole of a client's delivery twice to say
    the same thing.
    """
    out: dict[int, list[DailyPoint]] = {}
    for item in line_items:
        totals: dict[dt.date, list[float]] = {}
        for series in strategies.get(item.id, []):
            for date, values in series.metrics_by_date.items():
                bucket = totals.get(date)
                if bucket is None:
                    bucket = totals[date] = [0.0, 0.0, 0.0, 0.0]
                for index in range(4):
                    bucket[index] += values[index]
        if totals:
            out[item.id] = [
                DailyPoint(
                    date=date,
                    impressions=v[0], clicks=v[1], cost=v[2], conversions=v[3],
                )
                for date, v in sorted(totals.items())
            ]
    return out


def _daily_by_line_item(
    session, line_items: list[LineItem]
) -> dict[int, list[DailyPoint]]:
    """Delivery per line item per day, summed across its strategies."""
    join = _delivery_join(session, line_items)
    if not join:
        return {}

    stmt = (
        select(
            DailyDelivery.external_order_id,
            DailyDelivery.external_line_item_id,
            DailyDelivery.data_source,
            DailyDelivery.campaign_id,
            DailyDelivery.date,
            func.sum(DailyDelivery.impressions),
            func.sum(DailyDelivery.clicks),
            func.sum(DailyDelivery.cost),
            func.sum(DailyDelivery.conversions),
        )
        .where(join.where())
        .group_by(
            DailyDelivery.external_order_id,
            DailyDelivery.external_line_item_id,
            DailyDelivery.data_source,
            DailyDelivery.campaign_id,
            DailyDelivery.date,
        )
    )

    # A linked campaign is several rows here - one per day per campaign - and
    # the same day can also arrive by the id route, so days are added up
    # rather than appended as they come.
    totals: dict[int, dict[dt.date, list[float]]] = defaultdict(
        lambda: defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    )
    for (
        order_id, li_id, source, campaign, date, impressions, clicks, cost, conversions
    ) in session.execute(stmt):
        target = join.resolve(order_id, li_id, source, campaign)
        if target is None:
            continue
        bucket = totals[target][date]
        bucket[0] += float(impressions or 0)
        bucket[1] += float(clicks or 0)
        bucket[2] += float(cost or 0)
        bucket[3] += float(conversions or 0)

    out: dict[int, list[DailyPoint]] = {}
    for target, by_date in totals.items():
        out[target] = [
            DailyPoint(
                date=date,
                impressions=values[0],
                clicks=values[1],
                cost=values[2],
                conversions=values[3],
            )
            for date, values in sorted(by_date.items())
        ]
    return out


MAX_CHART_SERIES = 8

# Statuses that say an order has not started running yet.
NOT_LAUNCHED_STATUSES = {
    "io pending launch", "pending launch", "pending", "not launched",
    "io pending", "awaiting launch",
}


@dataclass
class StrategySeries:
    """One strategy's delivery, day by day.

    The line item is what was sold; the strategies under it are what actually
    ran, and a buyer needs to see them apart - retargeting behaving
    differently from behavioral is the thing worth catching.

    Grouped by targeting, not by the feed's strategy id: one line item can
    carry twenty ids that are the same targeting re-flighted, all named
    identically, and twenty indistinguishable lines answer nothing.
    """

    line_item_id: int
    label: str
    product: str | None
    by_date: dict[dt.date, float]
    total: float = 0.0
    # How many of the feed's strategy ids rolled up into this line.
    strategy_count: int = 0
    # Every metric, so the breakout can show performance without a second
    # query for each thing it wants to say.
    impressions: float = 0.0
    clicks: float = 0.0
    cost: float = 0.0
    conversions: float = 0.0
    # Every metric per day, so the order page's per-product daily totals can
    # be added up from these rather than fetched again by a second query
    # over the same rows.
    metrics_by_date: dict[dt.date, list[float]] = field(default_factory=dict)

    @property
    def ctr(self) -> float | None:
        return (self.clicks / self.impressions) if self.impressions else None


def strategy_series(
    session, line_items: list[LineItem], metric: str = "impressions"
) -> dict[int, list[StrategySeries]]:
    """Per-strategy daily delivery, grouped under each line item."""
    join = _delivery_join(session, line_items)
    if not join:
        return {}

    stmt = (
        select(
            DailyDelivery.external_order_id,
            DailyDelivery.external_line_item_id,
            DailyDelivery.data_source,
            DailyDelivery.campaign_id,
            DailyDelivery.strategy_id,
            DailyDelivery.strategy_name,
            DailyDelivery.strategy_type,
            DailyDelivery.product,
            DailyDelivery.date,
            func.sum(DailyDelivery.impressions),
            func.sum(DailyDelivery.clicks),
            func.sum(DailyDelivery.cost),
            func.sum(DailyDelivery.conversions),
        )
        .where(join.where())
        .group_by(
            DailyDelivery.external_order_id,
            DailyDelivery.external_line_item_id,
            DailyDelivery.data_source,
            DailyDelivery.campaign_id,
            DailyDelivery.strategy_id,
            DailyDelivery.strategy_name,
            DailyDelivery.strategy_type,
            DailyDelivery.product,
            DailyDelivery.date,
        )
    )

    collected: dict[tuple[int, str], StrategySeries] = {}
    seen_ids: dict[tuple[int, str], set[str]] = defaultdict(set)
    for (
        order_id, li_id, source, campaign, strategy_id, strategy_name,
        strategy_type, product, date,
        impressions, clicks, cost, conversions,
    ) in session.execute(stmt):
        target = join.resolve(order_id, li_id, source, campaign)
        if target is None:
            continue
        metrics = {
            "impressions": float(impressions or 0),
            "clicks": float(clicks or 0),
            "cost": float(cost or 0),
            "conversions": float(conversions or 0),
        }

        client = next(
            (li.order.client.name for li in line_items if li.id == target), None
        )
        label = line_item_label(product, strategy_type, strategy_name, client)
        key = (target, label)
        seen_ids[key].add(strategy_id)

        series = collected.get(key)
        if series is None:
            series = StrategySeries(
                line_item_id=target, label=label, product=product, by_date={}
            )
            collected[key] = series
        amount = metrics[metric]
        series.by_date[date] = series.by_date.get(date, 0.0) + amount
        series.total += amount
        series.impressions += metrics["impressions"]
        series.clicks += metrics["clicks"]
        series.cost += metrics["cost"]
        series.conversions += metrics["conversions"]
        bucket = series.metrics_by_date.get(date)
        if bucket is None:
            bucket = series.metrics_by_date[date] = [0.0, 0.0, 0.0, 0.0]
        bucket[0] += metrics["impressions"]
        bucket[1] += metrics["clicks"]
        bucket[2] += metrics["cost"]
        bucket[3] += metrics["conversions"]

    for key, series in collected.items():
        series.strategy_count = len(seen_ids[key])

    out: dict[int, list[StrategySeries]] = defaultdict(list)
    for series in collected.values():
        out[series.line_item_id].append(series)
    for items in out.values():
        items.sort(key=lambda s: -s.total)
    return out


@dataclass
class MonthServe:
    """One month of an order's delivery, against what it sold for it.

    Two figures, because an order can carry both kinds of line. Impressions
    and dollars were being added into one number and shown under one goal:
    a month that served 110,700 impressions and spent $19,000 read as
    110,700 of an impressions goal that had the dollars folded into it.
    """

    label: str
    start: dt.date
    served: float
    goal: float
    is_money: bool = False
    partial: bool = False
    # The other kind of line on the same order, kept apart.
    spent: float = 0.0
    spend_goal: float = 0.0

    @property
    def ratio(self) -> float | None:
        return (self.served / self.goal) if self.goal else None

    @property
    def spend_ratio(self) -> float | None:
        return (self.spent / self.spend_goal) if self.spend_goal else None

    @property
    def has_spend(self) -> bool:
        return bool(self.spent or self.spend_goal)

    @property
    def health(self) -> str:
        if self.ratio is None:
            return "unknown"
        return health(1.0 - self.ratio)

    @property
    def spend_health(self) -> str:
        if self.spend_ratio is None:
            return "unknown"
        return health(1.0 - self.spend_ratio)


def row_metric(row: PacingRow) -> str:
    """Which delivery figure a row is read in.

    Not the order's. An order carrying Display alongside Performance Max has
    one line counted in impressions and another in dollars, and reading both
    off the order's own type shows one of them somebody else's number.
    """
    return "cost" if row.is_money else "impressions"


def month_serve(view: "OrderView") -> list[MonthServe]:
    """Serve per calendar month, for the tiles across the top.

    A month is how the buying team thinks about pacing - the sold figures
    are monthly and the conversations are monthly - and reading it off a
    grid of fifty columns is work nobody should have to do.
    """
    main = view.metric
    served: dict[tuple[int, int], float] = defaultdict(float)
    spent: dict[tuple[int, int], float] = defaultdict(float)

    for row in view.rows:
        # A row read in the order's own metric fills the headline; anything
        # sold the other way is a spend line and gets its own figure.
        here = served if row_metric(row) == main else spent
        gross = row.client_cost_ratio if row.pacing_type == "event" else 1.0
        attr = row_metric(row)
        for point in view.daily_by_line_item.get(row.line_item_id, []):
            here[(point.date.year, point.date.month)] += (
                getattr(point, attr) * gross
            )

    months = sorted(set(served) | set(spent))
    if not months:
        return []

    covers = view.covers_from
    out = []
    for (year, month) in months:
        start = dt.date(year, month, 1)
        last = month_bounds(start)[1]
        # What was sold *for this month* - the lines whose flight covers it.
        # Summing every line the order ever carried put two Display lines
        # that finished in May into September's goal, which read as the
        # month delivering 9% of a target it was never given.
        goal = spend_goal = 0.0
        for row in view.rows:
            if row.start_date and row.start_date > last:
                continue
            if row.end_date and row.end_date < start:
                continue
            if row_metric(row) == main:
                goal += row.monthly_target
            else:
                spend_goal += row.monthly_target
        out.append(
            MonthServe(
                label=start.strftime("%b %Y"),
                start=start,
                served=served[(year, month)],
                goal=goal,
                spent=spent[(year, month)],
                spend_goal=spend_goal,
                is_money=main != "impressions",
                # The feed is a rolling window, so the first month it covers
                # is short by whatever ran before it.
                partial=bool(covers and covers > start),
            )
        )
    return out


@dataclass
class PacingGroup:
    """The rows on an order that pace one particular way."""

    pacing_type: str
    rows: list[PacingRow]
    total: PacingRow

    @property
    def open_rows(self) -> list[PacingRow]:
        return [r for r in self.rows if r.is_open]

    @property
    def label(self) -> str:
        return {
            "impression": "Impressions",
            "click": "Ad spend",
            "event": "Client budget",
        }.get(self.pacing_type, self.pacing_type.capitalize())


@dataclass
class OrderView:
    order: Order
    client: Client
    rows: list[PacingRow]
    total: PacingRow
    as_of: dt.date | None
    grid_dates: list[dt.date] = field(default_factory=list)
    grid: dict[int, dict[dt.date, float]] = field(default_factory=dict)
    on_pace_daily: float = 0.0
    # What a day should have served, for the month that day falls in. A
    # flight sells a figure per month, so one number across the whole flight
    # is the wrong thing to hold a September day against.
    on_pace_by_date: dict[dt.date, float] = field(default_factory=dict)
    # Per product, per day: how that day did against that day's target.
    grid_health: dict[int, dict[dt.date, str]] = field(default_factory=dict)
    covers_from: dt.date | None = None
    strategies: dict[int, list["StrategySeries"]] = field(default_factory=dict)
    metric: str = "impressions"
    daily_by_line_item: dict[int, list[DailyPoint]] = field(default_factory=dict)
    # (product label, [(strategy label, {date: value})]) for the daily grid.
    strategy_grid: list = field(default_factory=list)
    grid_range: str = "month"
    # Which ranges each day belongs to, for the grid's range buttons.
    range_classes: dict[dt.date, str] = field(default_factory=dict)
    month_days: int = 0
    # Which ranges each day belongs to, for the grid's range buttons.
    range_classes: dict[dt.date, str] = field(default_factory=dict)
    month_days: int = 0
    # The rows split by how they pace, order's type first. Impressions and
    # dollars need different columns and cannot share a Total, so an order
    # carrying both gets a table each rather than one table that lies about
    # one of them.
    groups: list["PacingGroup"] = field(default_factory=list)
    # The same table, counting only the lines still running. An order runs as
    # long as its longest line, so a table of every line it ever carried
    # totals a flight nobody is still buying.
    open_total: PacingRow | None = None
    closed_total: PacingRow | None = None

    @property
    def open_rows(self) -> list[PacingRow]:
        return [r for r in self.rows if r.is_open]

    @property
    def closed_rows(self) -> list[PacingRow]:
        return [r for r in self.rows if not r.is_open]

    @property
    def default_scope(self) -> str:
        """Which tab opens. Running, unless nothing is."""
        return "open" if self.open_rows else "closed"

    @property
    def health(self) -> str:
        return health(self.total.month_pacing_pct)

    @property
    def partial_history(self) -> bool:
        """True when the flight started before the feed's earliest day."""
        return bool(
            self.covers_from
            and self.order.start_date
            and self.order.start_date < self.covers_from
        )

    @property
    def status_contradicts_delivery(self) -> bool:
        """The order says it has not launched, and yet it has delivered.

        Both cannot be true. Either the status is stale in the export - the
        usual case, an order that went live without anybody moving it on -
        or this delivery is being read onto the wrong order, which would
        make every figure on the page wrong. Worth saying either way, and
        not something to pick between silently.
        """
        status = (self.order.status or "").strip().lower()
        return bool(NOT_LAUNCHED_STATUSES & {status} and self.total.to_date)


# How much of the flight the daily grid shows. A year-long flight is
# hundreds of columns and the interesting ones are always at the end.
GRID_RANGES = (
    ("month", "This month"),
    ("30", "Last 30 days"),
    ("flight", "Whole flight"),
)


def day_log(session, order_id: int) -> list["DayNote"]:
    """The running commentary on an order, newest day first."""
    return list(
        session.execute(
            select(DayNote)
            .where(DayNote.order_id == order_id)
            .order_by(DayNote.date.desc(), DayNote.id.desc())
        ).scalars()
    )


def order_view(
    session,
    order_id: int,
    as_of: dt.date | None = None,
    grid_range: str = "month",
) -> OrderView | None:
    order = session.execute(
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.line_items), selectinload(Order.client))
    ).scalar_one_or_none()
    if order is None:
        return None

    as_of = as_of or latest_delivery_date(session) or dt.date.today()
    covers_from = earliest_delivery_date(session)
    # Website Visitor ID, Live Chat, SEO, reputation management and the
    # management-fee lines sit on orders but have no impressions or spend to
    # pace, so they never reach a pacing view.
    line_items = [
        li for li in sorted(order.line_items, key=lambda li: (li.sort_order, li.id))
        if products.is_paced(li.product)
    ]
    # One pass over the delivery, not two. The strategies under a line item
    # are the same rows as the line item's own days, so adding them up here
    # costs nothing and saves a second aggregate over the same table.
    attr = "impressions" if order.pacing_type == "impression" else "cost"
    strategies: dict[int, list[StrategySeries]] = {}
    for metric in ("impressions", "cost"):
        wanted = [
            li for li in line_items
            if (
                "impressions"
                if (li.pacing_type or order.pacing_type or "impression") == "impression"
                else "cost"
            ) == metric
        ]
        if wanted:
            strategies.update(strategy_series(session, wanted, metric=metric))

    daily = _daily_from_strategies(line_items, strategies)

    rows = [compute_row(li, order, daily.get(li.id, []), as_of) for li in line_items]
    total = total_row(rows, order.pacing_type)
    open_total = total_row([r for r in rows if r.is_open], order.pacing_type)
    closed_total = total_row([r for r in rows if not r.is_open], order.pacing_type)

    # The daily grid runs the length of the flight, so a buyer can see which
    # days actually served - which is the whole point of the hand-kept sheet.
    # The whole flight, always. Each day is tagged with the ranges it falls
    # in, so the range buttons hide columns rather than fetching the page
    # again - which is what made them feel slow.
    grid_dates: list[dt.date] = []
    range_classes: dict[dt.date, str] = {}
    month_days = 0
    if order.start_date and order.end_date:
        first = max(order.start_date, covers_from) if covers_from else order.start_date
        last = min(order.end_date, as_of)
        month_start = month_bounds(as_of)[0]
        thirty = last - dt.timedelta(days=29)
        day = first
        while day <= last:
            grid_dates.append(day)
            tags = []
            if day >= month_start:
                tags.append("in-month")
                month_days += 1
            if day >= thirty:
                tags.append("in-30")
            range_classes[day] = " ".join(tags)
            day += dt.timedelta(days=1)

    # Each row in its own metric. The grid used the order's, so on an order
    # sold in impressions every spend line showed its impression count in
    # the money column - a Performance Max day that spent $8.77 read $293.00,
    # which was its impressions.
    grid = {
        row.line_item_id: {
            p.date: getattr(p, row_metric(row)) for p in row.daily
        }
        for row in rows
        if row.line_item_id is not None
    }
    # Day by day, per strategy under each product - the grid a buyer reads
    # across to see which targeting stopped on which day.
    strategy_grid = [
        (
            row.label,
            [
                (label, series.by_date)
                for _, label, series, _ in group_by_targeting(
                    row.line_item.product if row.line_item else None,
                    strategies.get(row.line_item_id, []),
                )
            ],
        )
        for row in rows
        if row.line_item_id is not None
    ]

    # What each day should have served. The daily target is the month's sold
    # figure over the days of the flight that fall in that month - which is
    # what the tiles and the month pacing above both work in. Held against
    # the life-of-flight daily figure instead, a month serving exactly to
    # plan read as doubling its target.
    on_pace_by_date: dict[dt.date, float] = {}
    grid_health: dict[int, dict[dt.date, str]] = defaultdict(dict)
    for day in grid_dates:
        window = month_window(day, order.start_date, order.end_date) if (
            order.start_date and order.end_date
        ) else None
        days = inclusive_days(*window) if window else 0
        on_pace_by_date[day] = (total.monthly_target / days) if days else 0.0

    for row in rows:
        if row.line_item_id is None or not row.monthly_target:
            continue
        served = {p.date: getattr(p, row_metric(row)) for p in row.daily}
        for day in grid_dates:
            window = month_window(day, row.start_date, row.end_date) if (
                row.start_date and row.end_date
            ) else None
            days = inclusive_days(*window) if window else 0
            if not days:
                continue
            target = row.monthly_target / days
            value = served.get(day)
            if value is None:
                continue
            grid_health[row.line_item_id][day] = health(
                1.0 - (value / target) if target else None
            )

    # Kept so the linking section does not aggregate the same delivery again.
    daily_by_line_item = {
        row.line_item_id: row.daily for row in rows if row.line_item_id is not None
    }

    # The order's own type first: it is what the tiles above describe.
    order_type = order.pacing_type or "impression"
    seen = [order_type] + sorted(
        {r.pacing_type for r in rows if r.pacing_type != order_type}
    )
    groups = [
        PacingGroup(
            pacing_type=kind,
            rows=[r for r in rows if r.pacing_type == kind],
            total=total_row([r for r in rows if r.pacing_type == kind], kind),
        )
        for kind in seen
    ]
    groups = [g for g in groups if g.rows]

    return OrderView(
        order=order,
        client=order.client,
        rows=rows,
        total=total,
        open_total=open_total,
        closed_total=closed_total,
        groups=groups,
        as_of=as_of,
        grid_dates=grid_dates,
        grid=grid,
        on_pace_daily=total.daily_target,
        on_pace_by_date=on_pace_by_date,
        grid_health=grid_health,
        covers_from=covers_from,
        strategies=strategies,
        metric=attr,
        daily_by_line_item=daily_by_line_item,
        strategy_grid=strategy_grid,
        grid_range=grid_range,
        range_classes=range_classes,
        month_days=month_days,
    )


@dataclass
class ProductChip:
    """One product on an order, as the pill in the Products column.

    Carries its own served and expected so hovering the pill answers
    "how is this one doing" without opening the order.
    """

    code: str
    name: str
    hex: str = "#123A63"
    text_hex: str = "#FFFFFF"
    served: float = 0.0
    expected: float = 0.0
    goal: float = 0.0
    is_money: bool = False

    @property
    def ratio(self) -> float | None:
        return (self.served / self.expected) if self.expected else None

    @property
    def health(self) -> str:
        if self.ratio is None:
            return "unknown"
        # Same tolerance as everywhere else, read the other way round: the
        # ratio is 1.0 on pace rather than 0.0.
        return health(1.0 - self.ratio)


@dataclass
class OverviewRow:
    order: Order
    client: Client
    total: PacingRow
    as_of: dt.date
    products: list[ProductChip] = field(default_factory=list)
    covers_from: dt.date | None = None

    @property
    def health(self) -> str:
        return health(self.total.month_pacing_pct)

    @property
    def partial_history(self) -> bool:
        return bool(
            self.covers_from
            and self.order.start_date
            and self.order.start_date < self.covers_from
        )

    @property
    def is_cancelled(self) -> bool:
        return (self.order.status or "").strip().lower() in {"cancelled", "canceled"}

    @property
    def total_health(self) -> str:
        return health(self.total.pacing_pct)


def _query_clause(query: str):
    """What the search box matches: the client, the order, or its id.

    The id was not among them, so typing the number off an order page found
    nothing and the only way back to an order was a link you already had.
    """
    text = query.strip()
    like = f"%{text}%"
    return (
        Client.name.ilike(like)
        | Order.name.ilike(like)
        | Order.external_order_id.ilike(like)
    )


@dataclass
class HiddenOrder:
    """An order the search matched and the filters then dropped."""

    order: Order
    client: Client
    reason: str


def hidden_matches(
    session,
    query: str,
    shown: set[int],
    as_of: dt.date,
    include_ended: bool = False,
    include_non_io: bool = False,
) -> list[HiddenOrder]:
    """Orders the search found that the page is not showing, and why.

    Four of the filters drop an order without leaving a trace, and two of
    them key off values the export changes under you - an order worked on
    all week stops appearing and nothing anywhere says where it went.

    `shown` is what the page already lists, so this reports only the gap.
    """
    if not query.strip():
        return []

    orders = [
        order
        for order in session.execute(
            select(Order)
            .join(Client, Client.id == Order.client_id)
            .where(_query_clause(query))
            .options(selectinload(Order.client))
        ).scalars()
        if order.id not in shown
    ]

    out: list[HiddenOrder] = []
    for order in orders:
        kind = (order.order_type or "").strip().lower()
        status = (order.status or "").strip().lower()
        if not include_non_io and kind != PACEABLE_ORDER_TYPE:
            reason = order.order_type or "no order type"
        elif not order.active:
            reason = order.status or "never ran"
        elif not include_ended and order.end_date and order.end_date < as_of:
            reason = f"ended {order.end_date:%-d %b %Y}"
        elif status in {"cancelled", "canceled"}:
            reason = "cancelled, nothing delivered"
        else:
            reason = "filtered"
        out.append(HiddenOrder(order=order, client=order.client, reason=reason))
    return out


def overview(
    session,
    as_of: dt.date | None = None,
    buyer: str | None = None,
    market: str | None = None,
    pacing_type: str | None = None,
    query: str | None = None,
    include_ended: bool = False,
    needs_terms: bool | None = None,
    include_non_io: bool = False,
) -> list[OverviewRow]:
    """One computed line per order, which is the summary tab.

    Only Insertion Orders are paced. Other order types are on the books but
    are not what the buying team works, so they are out unless asked for.
    """
    as_of = as_of or latest_delivery_date(session) or dt.date.today()
    covers_from = earliest_delivery_date(session)

    stmt = (
        select(Order)
        .join(Client, Client.id == Order.client_id)
        .options(selectinload(Order.line_items), selectinload(Order.client))
    )
    if buyer:
        stmt = stmt.where(Order.buyer == buyer)
    if market:
        stmt = stmt.where(Client.market == market)
    if pacing_type:
        stmt = stmt.where(Order.pacing_type == pacing_type)
    if query:
        stmt = stmt.where(_query_clause(query))
    if not include_non_io:
        stmt = stmt.where(
            func.lower(func.coalesce(Order.order_type, "")) == PACEABLE_ORDER_TYPE
        )
    # An order that never ran has nothing to pace and no delivery to show.
    stmt = stmt.where(Order.active.is_(True))
    if not include_ended:
        stmt = stmt.where((Order.end_date.is_(None)) | (Order.end_date >= as_of))

    orders = list(session.execute(stmt).scalars())
    all_line_items = [
        li for o in orders for li in o.line_items if products.is_paced(li.product)
    ]
    # Summed in the database: this page never shows a day, and fetching every
    # day of every line item to add them up here was most of its wait.
    totals, spilled = _totals_by_line_item(session, all_line_items, as_of)
    # Whatever ran outside its own flight still has to be worked out day by
    # day, because only that can tell the two apart.
    daily = _daily_by_line_item(session, spilled) if spilled else {}

    out: list[OverviewRow] = []
    for order in orders:
        line_items = [
            li for li in sorted(order.line_items, key=lambda li: (li.sort_order, li.id))
            if products.is_paced(li.product)
        ]
        rows = [
            compute_row(
                li, order, daily.get(li.id, []), as_of, totals=totals.get(li.id)
            )
            for li in line_items
        ]
        total = total_row(rows, order.pacing_type)

        # One pill per product, summing the line items that share it.
        chips: dict[str, ProductChip] = {}
        for item, row in zip(line_items, rows):
            name = item.product or "Other"
            chip = chips.get(name)
            if chip is None:
                entry = products.lookup(name)
                chip = ProductChip(
                    code=products.abbreviation(name),
                    name=name,
                    hex=entry.hex if entry else "#123A63",
                    text_hex=entry.text_hex if entry else "#FFFFFF",
                    is_money=row.is_money,
                )
                chips[name] = chip
            chip.served += row.to_date
            chip.expected += row.on_pace
            chip.goal += row.total_target

        out.append(
            OverviewRow(
                order=order,
                client=order.client,
                total=total,
                as_of=as_of,
                products=sorted(chips.values(), key=lambda c: -c.goal),
                covers_from=covers_from,
            )
        )

    # Cancelled orders are kept, but only the ones that ran before they were
    # cancelled are worth a line - the rest never served at all.
    out = [
        r for r in out
        if not r.is_cancelled or r.total.impressions or r.total.cost
    ]

    if needs_terms is True:
        out = [r for r in out if r.total.needs_setup]
    elif needs_terms is False:
        out = [r for r in out if not r.total.needs_setup]

    # Worst pacing first - that is the list a buyer works down. Orders with no
    # sold terms yet cannot be paced, so they sort to the bottom.
    out.sort(
        key=lambda r: (
            r.total.month_pacing_pct is None,
            -abs(r.total.month_pacing_pct or 0),
        )
    )
    return out


@dataclass
class PageTotal:
    """The `Pacing total` line under the table."""

    month_to_date: float = 0.0
    month_on_pace: float = 0.0
    monthly_target: float = 0.0
    to_date: float = 0.0
    on_pace: float = 0.0
    total_target: float = 0.0
    avg_daily: float = 0.0
    daily_needed: float = 0.0

    @property
    def delivery_ratio(self) -> float | None:
        return (self.to_date / self.on_pace) if self.on_pace else None

    @property
    def month_delivery_ratio(self) -> float | None:
        return (self.month_to_date / self.month_on_pace) if self.month_on_pace else None

    @property
    def health(self) -> str:
        r = self.delivery_ratio
        return health(1.0 - r) if r is not None else "unknown"

    @property
    def month_health(self) -> str:
        r = self.month_delivery_ratio
        return health(1.0 - r) if r is not None else "unknown"


def page_total(rows: list[OverviewRow]) -> PageTotal:
    """Sum a page of orders.

    Impression and spend orders are summed separately in real life, but the
    table mixes them, so this counts only the impression ones - adding
    dollars to impressions would produce a number that means nothing.
    """
    out = PageTotal()
    for item in rows:
        t = item.total
        if t.is_money:
            continue
        out.month_to_date += t.month_to_date
        out.month_on_pace += t.month_on_pace
        out.monthly_target += t.monthly_target
        out.to_date += t.to_date
        out.on_pace += t.on_pace
        out.total_target += t.total_target
        out.avg_daily += t.avg_daily or 0.0
        out.daily_needed += t.daily_needed or 0.0
    return out


def filter_options(session) -> dict[str, list[str]]:
    buyers = [
        b for (b,) in session.execute(
            select(Order.buyer).where(Order.buyer.isnot(None)).distinct()
        )
    ]
    markets = [
        m for (m,) in session.execute(
            select(Client.market).where(Client.market.isnot(None)).distinct()
        )
    ]
    return {"buyers": sorted(buyers), "markets": sorted(markets)}


def chart_series(view: "OrderView", limit: int = MAX_CHART_SERIES) -> dict:
    """Flatten an order's strategies into something the chart can draw.

    Categorical colour carries eight series; past that the tail folds into a
    single "Other" rather than the palette being cycled, which would give two
    strategies the same colour.
    """
    everything: list[StrategySeries] = []
    for items in view.strategies.values():
        everything.extend(items)
    everything.sort(key=lambda s: -s.total)

    dates = sorted({d for s in everything for d in s.by_date})
    if not dates or not everything:
        return {"dates": [], "series": [], "metric": view.metric}

    head, tail = everything[:limit], everything[limit:]
    series = [
        {"label": s.label, "values": [s.by_date.get(d, 0.0) for d in dates]}
        for s in head
    ]
    if tail:
        series.append({
            "label": f"Other ({len(tail)} strategies)",
            "values": [sum(s.by_date.get(d, 0.0) for s in tail) for d in dates],
        })

    # Deliberately no daily-target line: the target is the whole order's,
    # and drawing it against per-strategy lines invites reading a single
    # strategy as "behind" when the order as a whole is fine. Pacing against
    # the target is what the table above the chart is for.
    return {
        "dates": [d.isoformat() for d in dates],
        "series": series,
        "metric": view.metric,
    }


# What the performance chart draws. Visits are not among them: the delivery
# feed carries impressions, clicks, conversions, viewthroughs and click
# conversions, and nothing that counts a visit.
PERFORMANCE_METRICS = (
    ("clicks", "Clicks", "count"),
    ("conversions", "Conversions", "count"),
    ("ctr", "CTR", "percent"),
)


def performance_chart(view: "OrderView") -> dict:
    """CTR, clicks and conversions on one chart, for the order as a whole.

    One frame, because they are read against each other: a CTR that climbs
    while conversions fall is the thing worth seeing, and it is invisible
    when each sits behind its own button.

    Two axes, because they are not the same kind of number. Clicks and
    conversions are counts; CTR is a ratio in the low single-digit percent.
    On one axis the CTR line is flat against the floor and says nothing.
    """
    points: dict[dt.date, list[float]] = {}
    for row in view.rows:
        for point in view.daily_by_line_item.get(row.line_item_id, []):
            bucket = points.get(point.date)
            if bucket is None:
                bucket = points[point.date] = [0.0, 0.0, 0.0]
            bucket[0] += point.impressions
            bucket[1] += point.clicks
            bucket[2] += point.conversions

    if not points:
        return {"dates": [], "series": []}

    days = sorted(points)
    clicks = [points[d][1] for d in days]
    conversions = [points[d][2] for d in days]
    ctr = [
        (points[d][1] / points[d][0]) if points[d][0] else 0.0 for d in days
    ]

    series = []
    if any(clicks):
        series.append({"label": "Clicks", "values": clicks, "axis": "left"})
    if any(conversions):
        series.append(
            {"label": "Conversions", "values": conversions, "axis": "left"}
        )
    if any(ctr):
        series.append(
            {"label": "CTR", "values": ctr, "axis": "right", "format": "percent"}
        )

    return {
        "dates": [d.isoformat() for d in days],
        "series": series,
        "metric": "count",
        "right_format": "percent",
    }


def product_charts(view: "OrderView", limit: int = MAX_CHART_SERIES) -> list[dict]:
    """Delivery per day, one line per product - and one chart per unit.

    Impressions for a product sold in impressions, client cost for one sold
    in spend. An order can carry both, and they cannot share an axis: a
    Display line running 40,000 a day beside a PPC line spending $90 would
    flatten the one against the other and say nothing about either. So a
    mixed order gets a chart each, which is also what the tiles above do.
    """
    ratios = {
        r.line_item_id: (r.client_cost_ratio if r.pacing_type == "event" else 1.0)
        for r in view.rows
        if r.line_item_id is not None
    }

    charts: list[dict] = []
    for group in view.groups:
        attr = "impressions" if group.pacing_type == "impression" else "cost"
        labelled: list[tuple[str, dict[dt.date, float], float]] = []
        for row in group.rows:
            if row.line_item_id is None:
                continue
            gross = ratios.get(row.line_item_id, 1.0)
            points = view.daily_by_line_item.get(row.line_item_id, [])
            by_date = {p.date: getattr(p, attr) * gross for p in points}
            total = sum(by_date.values())
            if total:
                labelled.append((row.label, by_date, total))
        labelled.sort(key=lambda item: -item[2])

        dates = sorted({d for _, by_date, _ in labelled for d in by_date})
        if not dates or not labelled:
            continue

        head, tail = labelled[:limit], labelled[limit:]
        series = [
            {"label": label, "values": [by_date.get(d, 0.0) for d in dates]}
            for label, by_date, _ in head
        ]
        if tail:
            series.append({
                "label": f"Other ({len(tail)} products)",
                "values": [
                    sum(by_date.get(d, 0.0) for _, by_date, _ in tail) for d in dates
                ],
            })
        charts.append({
            "label": group.label,
            "unit": (
                "Impressions per day"
                if attr == "impressions"
                else "Client cost per day"
            ),
            "dates": [d.isoformat() for d in dates],
            "series": series,
            "metric": attr,
        })
    return charts


@dataclass
class StrategyPacing:
    """A strategy's sold split beside what actually ran under it.

    The sold side comes from the seeded sheets, the delivered side from the
    feed. They are paired on the targeting rather than the whole label,
    because the two name products differently - "FB/IG - Category" against
    "FB - Category Facebook".
    """

    label: str
    match_key: str | None
    monthly_target: float | None
    total_target: float | None
    rate: float | None
    delivered: float = 0.0
    matched_labels: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> float | None:
        if self.total_target is None:
            return None
        return self.total_target - self.delivered

    @property
    def share(self) -> float | None:
        """How much of what was sold has run."""
        if not self.total_target:
            return None
        return self.delivered / self.total_target


def strategy_pacing(session, view: "OrderView") -> list[StrategyPacing]:
    """The order's sold strategy split, with delivery matched onto it."""
    terms = sorted(
        session.execute(
            select(StrategyTerms).where(StrategyTerms.order_id == view.order.id)
        ).scalars(),
        key=lambda t: (t.sort_order, t.id),
    )
    if not terms:
        return []

    # Delivered, keyed by the targeting that ran it.
    delivered: dict[str, float] = defaultdict(float)
    labels: dict[str, list[str]] = defaultdict(list)
    for items in view.strategies.values():
        for series in items:
            key = sheets.match_key(series.label)
            if key:
                delivered[key] += series.total
                labels[key].append(series.label)

    out = []
    for term in terms:
        out.append(
            StrategyPacing(
                label=term.label,
                match_key=term.match_key,
                monthly_target=term.monthly_target,
                total_target=term.total_target,
                rate=term.rate,
                delivered=delivered.get(term.match_key or "", 0.0),
                matched_labels=sorted(set(labels.get(term.match_key or "", []))),
            )
        )
    return out


# --------------------------------------------------------------------------
# Campaign linking
# --------------------------------------------------------------------------
@dataclass
class CampaignCandidate:
    """A DSP campaign in the feed that could be what a line item bought."""

    data_source: str
    campaign_id: str
    campaign_name: str | None
    product: str | None
    external_line_item_id: str | None
    month_impressions: float = 0.0
    month_cost: float = 0.0
    impressions: float = 0.0
    cost: float = 0.0
    first_date: dt.date | None = None
    last_date: dt.date | None = None
    # The line item this campaign already reaches, and how it got there.
    taken_by: int | None = None
    taken_how: str | None = None  # "id" or "link"

    # Which net caught this one, so the dialog can say why it is listed.
    found_by: str = ""

    @property
    def key(self) -> str:
        return f"{self.data_source}␟{self.campaign_id}"

    @property
    def label(self) -> str:
        name = (self.campaign_name or "").strip()
        return name or f"{self.data_source} {self.campaign_id}"


@dataclass
class LinkRow:
    """One line item, and what delivery currently reaches it."""

    line_item: LineItem
    label: str
    code: str
    hex: str
    text_hex: str
    # A line item can be bought across several campaigns - a Meta line split
    # into an impressions campaign and a leads one is ordinary - so this is a
    # set, not a single choice. The other direction stays one-to-one: a
    # campaign belongs to one line item, or its delivery counts twice.
    links: list[CampaignLink] = field(default_factory=list)
    matched_campaigns: list[CampaignCandidate] = field(default_factory=list)
    served: float = 0.0
    is_money: bool = False

    @property
    def state(self) -> str:
        """What the row needs from a human, in one word.

        `linked` and `matched` are both fine; `unmatched` is the one that
        costs the buying team a wrong number on the page.
        """
        if self.links:
            return (
                "verified"
                if all(link.ops_verified for link in self.links)
                else "linked"
            )
        if self.matched_campaigns:
            return "matched"
        return "unmatched"

    @property
    def linked_by(self) -> str | None:
        for link in self.links:
            if link.linked_by:
                return link.linked_by
        return None

    @property
    def linked_keys(self) -> str:
        """The campaigns on this row, for the dialog to tick on open."""
        return ",".join(
            f"{link.data_source}\u241f{link.campaign_id}" for link in self.links
        )


@dataclass
class LinkingView:
    rows: list[LinkRow]
    candidates: list[CampaignCandidate]
    as_of: dt.date

    @property
    def unmatched_count(self) -> int:
        return sum(1 for r in self.rows if r.state == "unmatched")

    @property
    def free_candidates(self) -> list[CampaignCandidate]:
        """Campaigns nothing on this order is already reading."""
        return [c for c in self.candidates if c.taken_by is None]


def campaign_candidates(
    session, order: Order, join: "_Join", as_of: dt.date
) -> list[CampaignCandidate]:
    """Every campaign in the feed that plausibly belongs to this order.

    Three nets, because none of them is reliable on its own:

    * the **client name** exactly as the feed writes it. A campaign built
      before the order was written carries the client but no order id,
      which is the case linking exists for.
    * the **order id**, which catches a campaign the feed files under a
      differently-spelled client - the shop that is "Peters Heating & Air"
      on one side and "Peters Heating and Air Conditioning" on the other.
    * the **order number appearing in the campaign's own name**, which is
      how the buying team names them and the only net that catches a
      campaign where both of the other two are wrong.

    Each candidate says which net caught it, because "why is this one here"
    and "why is that one not" are the two questions the dialog gets asked.
    """
    conditions = []
    reasons: list[tuple[str, object]] = []
    if order.client and order.client.name:
        clause = DailyDelivery.client_name == order.client.name
        conditions.append(clause)
        reasons.append(("client", clause))
    if order.external_order_id:
        clause = DailyDelivery.external_order_id == order.external_order_id
        conditions.append(clause)
        reasons.append(("order id", clause))
        # The team writes the order number into the campaign name.
        named = DailyDelivery.campaign_name.like(f"%{order.external_order_id}%")
        conditions.append(named)
        reasons.append(("named", named))
    if not conditions:
        return []

    # The calendar month, not the flight's slice of it: a candidate campaign
    # is not yet attached to a flight, so the flight's dates say nothing
    # about it.
    month_start, _ = month_bounds(as_of)

    # Month to date and lifetime in one pass. Two queries over a client's
    # whole delivery to show one table was half the order page's wait.
    in_month = (DailyDelivery.date >= month_start) & (DailyDelivery.date <= as_of)
    month_impressions = case((in_month, DailyDelivery.impressions), else_=0.0)
    month_cost = case((in_month, DailyDelivery.cost), else_=0.0)

    stmt = (
        select(
            DailyDelivery.data_source,
            DailyDelivery.campaign_id,
            func.max(DailyDelivery.campaign_name),
            func.max(DailyDelivery.product),
            func.max(DailyDelivery.external_line_item_id),
            func.sum(DailyDelivery.impressions),
            func.sum(DailyDelivery.cost),
            func.min(DailyDelivery.date),
            func.max(DailyDelivery.date),
            func.sum(month_impressions),
            func.sum(month_cost),
            func.max(DailyDelivery.client_name),
        )
        .where(or_(*conditions))
        .group_by(DailyDelivery.data_source, DailyDelivery.campaign_id)
    )

    out: list[CampaignCandidate] = []
    for (
        source, campaign, name, product, li_id, impressions, cost, first, last,
        month_impr, month_spend, client_name,
    ) in session.execute(stmt):
        candidate = CampaignCandidate(
            data_source=source,
            campaign_id=campaign,
            campaign_name=name,
            product=product,
            external_line_item_id=li_id,
            month_impressions=float(month_impr or 0),
            month_cost=float(month_spend or 0),
            impressions=float(impressions or 0),
            cost=float(cost or 0),
            first_date=first,
            last_date=last,
            found_by=_found_by(order, client_name, li_id, name),
        )
        by_id = join.resolve(order.external_order_id, li_id)
        if by_id is not None:
            candidate.taken_by, candidate.taken_how = by_id, "id"
        elif (source, campaign) in join.by_link:
            candidate.taken_by = join.by_link[(source, campaign)]
            candidate.taken_how = "link"
        # A campaign that has never delivered cannot be what a line item is
        # reading, and the feed carries rollup rows that are not campaigns at
        # all. Anything already attached stays listed whatever it has served,
        # so a link is never silently unpickable.
        if not (candidate.impressions or candidate.cost) and candidate.taken_by is None:
            continue
        out.append(candidate)

    out.sort(key=lambda c: (-c.month_impressions, -c.impressions, c.label))
    return out


def linking_view(
    session,
    order: Order,
    as_of: dt.date | None = None,
    daily: dict[int, list[DailyPoint]] | None = None,
) -> LinkingView:
    """What each line item is reading, and what is going unread.

    Non-paced products are left out here as everywhere else: there is no
    campaign to link a Live Chat line to.

    `daily` is the per-line-item delivery the order page has already worked
    out. Rebuilding it here meant every order page aggregated the same
    delivery twice, for the same numbers.
    """
    as_of = as_of or latest_delivery_date(session) or dt.date.today()
    line_items = [
        li for li in sorted(order.line_items, key=lambda li: (li.sort_order, li.id))
        if products.is_paced(li.product)
    ]
    join = _delivery_join(session, line_items)
    candidates = campaign_candidates(session, order, join, as_of)
    if daily is None:
        daily = _daily_by_line_item(session, line_items)

    links: dict[int, list[CampaignLink]] = defaultdict(list)
    for link in session.execute(
        select(CampaignLink)
        .where(CampaignLink.line_item_id.in_([li.id for li in line_items] or [0]))
        .order_by(CampaignLink.campaign_name, CampaignLink.campaign_id)
    ).scalars():
        links[link.line_item_id].append(link)

    is_money = order.pacing_type != "impression"
    rows: list[LinkRow] = []
    for item in line_items:
        entry = products.lookup(item.product)
        points = daily.get(item.id, [])
        rows.append(
            LinkRow(
                line_item=item,
                label=item.product or item.name,
                code=products.abbreviation(item.product),
                hex=entry.hex if entry else "#123A63",
                text_hex=entry.text_hex if entry else "#FFFFFF",
                links=links.get(item.id, []),
                matched_campaigns=[
                    c for c in candidates
                    if c.taken_by == item.id and c.taken_how == "id"
                ],
                served=sum(p.cost if is_money else p.impressions for p in points),
                is_money=is_money,
            )
        )

    return LinkingView(rows=rows, candidates=candidates, as_of=as_of)


# --------------------------------------------------------------------------
# Summed delivery, for pages that never show a day
# --------------------------------------------------------------------------
def _totals_by_line_item(
    session, line_items: list[LineItem], as_of: dt.date
) -> tuple[dict[int, DeliveryTotals], list[LineItem]]:
    """Per line item: lifetime and month-to-date, summed by the database.

    The overview shows no daily figures at all, so fetching each line item's
    days to add them up in Python was a third of a million rows to render a
    page of a hundred and fifty orders. Summed here, it is one row per line
    item.

    A row also has to know its delivery inside the flight, and the flight
    differs per line item so the database cannot window it in the same pass.
    It does not have to: where everything a line item ran falls inside its
    flight - which is the ordinary case - the flight sums are the lifetime
    sums, and the month-window sums are the calendar month's. Whatever ran
    outside its flight is handed back as the second return value, for the
    caller to work out day by day the slow way. Correct either way; the fast
    path just covers almost everything.
    """
    join = _delivery_join(session, line_items)
    if not join:
        return {}, []

    month_start, _ = month_bounds(as_of)
    in_month = (DailyDelivery.date >= month_start) & (DailyDelivery.date <= as_of)

    def month_of(column):
        return func.sum(case((in_month, column), else_=0.0))

    stmt = (
        select(
            DailyDelivery.external_order_id,
            DailyDelivery.external_line_item_id,
            DailyDelivery.data_source,
            DailyDelivery.campaign_id,
            func.sum(DailyDelivery.impressions),
            func.sum(DailyDelivery.clicks),
            func.sum(DailyDelivery.cost),
            func.sum(DailyDelivery.conversions),
            month_of(DailyDelivery.impressions),
            month_of(DailyDelivery.clicks),
            month_of(DailyDelivery.cost),
            month_of(DailyDelivery.conversions),
            func.min(DailyDelivery.date),
            func.max(DailyDelivery.date),
        )
        .where(join.where())
        .group_by(
            DailyDelivery.external_order_id,
            DailyDelivery.external_line_item_id,
            DailyDelivery.data_source,
            DailyDelivery.campaign_id,
        )
    )

    gathered: dict[int, list] = {}
    for (
        order_id, li_id, source, campaign,
        impressions, clicks, cost, conversions,
        m_impressions, m_clicks, m_cost, m_conversions,
        first, last,
    ) in session.execute(stmt):
        target = join.resolve(order_id, li_id, source, campaign)
        if target is None:
            continue
        bucket = gathered.get(target)
        if bucket is None:
            bucket = gathered[target] = [Totals(), Totals(), None, None]
        bucket[0].impressions += float(impressions or 0)
        bucket[0].clicks += float(clicks or 0)
        bucket[0].cost += float(cost or 0)
        bucket[0].conversions += float(conversions or 0)
        bucket[1].impressions += float(m_impressions or 0)
        bucket[1].clicks += float(m_clicks or 0)
        bucket[1].cost += float(m_cost or 0)
        bucket[1].conversions += float(m_conversions or 0)
        if first is not None:
            bucket[2] = first if bucket[2] is None else min(bucket[2], first)
        if last is not None:
            bucket[3] = last if bucket[3] is None else max(bucket[3], last)

    by_id = {li.id: li for li in line_items}
    out: dict[int, DeliveryTotals] = {}
    spilled: list[LineItem] = []
    for line_item_id, (every, month, first, last) in gathered.items():
        item = by_id.get(line_item_id)
        if item is None:
            continue
        order = item.order
        start = item.start_date or order.start_date
        end = item.end_date or order.end_date
        inside = (
            start is None
            or end is None
            or first is None
            or last is None
            or (start <= first and last <= end)
        )
        if not inside:
            spilled.append(item)
            continue
        out[line_item_id] = DeliveryTotals(
            every=every, in_flight=every, in_month=month
        )
    return out, spilled


# --------------------------------------------------------------------------
# Strategy level
# --------------------------------------------------------------------------
def sold_strategy_names(item: LineItem) -> list[str]:
    """The targeting the client bought on this product, as the order lists it.

    One cell, several strategies, and no agreement on the separator - the
    export writes commas, semicolons, slashes and newlines depending on who
    typed it.
    """
    raw = (item.sold_strategies or "").strip()
    if not raw:
        return []
    parts = re.split(r"[;,\n|/]+", raw)
    seen: list[str] = []
    for part in parts:
        name = part.strip(" -\t")
        if name and name.lower() not in {p.lower() for p in seen}:
            seen.append(name)
    return seen


@dataclass
class ObservedStrategy:
    """Targeting the feed is reporting under a product, and its share.

    What is actually running is knowable without anybody typing it. The
    split that is missing is the *sold* one - how much of the product each
    targeting was bought for - and the running shares are the obvious first
    draft of it.
    """

    label: str
    match_key: str | None
    delivered: float = 0.0
    share: float = 0.0
    # True when a sold row already claims this targeting.
    claimed: bool = False
    # True when the order says the client bought this targeting. A strategy
    # running that nobody bought, or bought and not running, are both worth
    # seeing - and only the order can say which is which.
    sold: bool = False
    # What the feed actually called it. The label above is the targeting -
    # "M - Retargeting" - which is how the buying team writes it; the feed
    # writes "FB - Retargeting Facebook Premium", and both are worth having.
    raw_labels: list[str] = field(default_factory=list)
    # What it cost, at the rate this strategy is set up at - which is the
    # product's, except on a merged-CPM product where each half has its own.
    spend: float = 0.0
    cpm: float | None = None
    days: int = 0
    # Performance, which is the other half of why a buyer opens this tab.
    impressions: float = 0.0
    clicks: float = 0.0
    conversions: float = 0.0

    @property
    def ctr(self) -> float | None:
        return (self.clicks / self.impressions) if self.impressions else None

    @property
    def per_day(self) -> float:
        return (self.delivered / self.days) if self.days else 0.0

    @property
    def spend_per_day(self) -> float:
        return (self.spend / self.days) if self.days else 0.0


@dataclass
class StrategyBlock:
    """One product, and the targeting bought under it.

    The hand-kept sheet is laid out this way - a product heading, its
    strategies beneath, then a Total row - because that is the grain a buyer
    adjusts at. A line item under-pacing says nothing about which targeting
    to push; this does.
    """

    line_item: LineItem
    label: str
    code: str
    hex: str
    text_hex: str
    rows: list[PacingRow] = field(default_factory=list)
    terms: list[StrategyTerms] = field(default_factory=list)
    total: PacingRow | None = None
    # Delivery under this product that no sold strategy accounts for.
    unclaimed: float = 0.0
    unclaimed_labels: list[str] = field(default_factory=list)
    # Every targeting the feed reports under this product, with its share.
    observed: list[ObservedStrategy] = field(default_factory=list)
    # What the order says the client bought on this product.
    sold_names: list[str] = field(default_factory=list)
    # Bought, but nothing running under it.
    not_running: list[str] = field(default_factory=list)

    @property
    def observed_total(self) -> float:
        return sum(o.delivered for o in self.observed)

    @property
    def observed_spend(self) -> float:
        return sum(o.spend for o in self.observed)

    @property
    def observed_clicks(self) -> float:
        return sum(o.clicks for o in self.observed)

    @property
    def observed_conversions(self) -> float:
        return sum(o.conversions for o in self.observed)

    @property
    def observed_ctr(self) -> float | None:
        impressions = sum(o.impressions for o in self.observed)
        return (self.observed_clicks / impressions) if impressions else None

    @property
    def observed_days(self) -> int:
        return max((o.days for o in self.observed), default=0)

    @property
    def observed_per_day(self) -> float:
        days = self.observed_days
        return (self.observed_total / days) if days else 0.0

    @property
    def observed_spend_per_day(self) -> float:
        days = self.observed_days
        return (self.observed_spend / days) if days else 0.0

    @property
    def rate(self) -> float:
        item = self.line_item
        return item.goal_cpm or item.goal_cpc or item.goal_cpe or 0.0


def group_by_targeting(
    product: str | None, running: list["StrategySeries"]
) -> list[tuple[str, str, "StrategySeries", list[str]]]:
    """Roll a product's delivery up by the targeting that ran it.

    The feed reports a campaign per audience and names each one in full;
    the buying team works in targeting. Used by the breakout and by the
    drafting both, so the two cannot end up calling the same thing by
    different names - which they did, and the drafted rows then matched
    nothing on the page they were drafted from.

    Returns (key, label, merged series, the feed's own names).
    """
    merged: dict[str, StrategySeries] = {}
    raw: dict[str, list[str]] = defaultdict(list)
    combined = combined_strategy(product)
    for series in running:
        key = (
            COMBINED_KEY if combined
            else sheets.match_key(series.label) or series.label.lower()
        )
        raw[key].append(series.label)
        existing = merged.get(key)
        if existing is None:
            merged[key] = StrategySeries(
                line_item_id=series.line_item_id,
                label=series.label,
                product=series.product,
                by_date=dict(series.by_date),
                total=series.total,
                impressions=series.impressions,
                clicks=series.clicks,
                cost=series.cost,
                conversions=series.conversions,
            )
            continue
        for date, value in series.by_date.items():
            existing.by_date[date] = existing.by_date.get(date, 0.0) + value
        existing.total += series.total
        existing.impressions += series.impressions
        existing.clicks += series.clicks
        existing.cost += series.cost
        existing.conversions += series.conversions

    return [
        (key, strategy_label(product, key, series.label), series,
         sorted(set(raw[key])))
        for key, series in merged.items()
    ]


def _found_by(order: Order, client_name, line_item_id, campaign_name) -> str:
    """Why this campaign is on the list."""
    reasons = []
    if order.client and client_name == order.client.name:
        reasons.append("client")
    order_id = order.external_order_id
    if order_id and campaign_name and order_id in str(campaign_name):
        reasons.append("named for this order")
    if not reasons:
        reasons.append("order id")
    return " · ".join(reasons)


# Products the platform runs as one campaign and reports as one line.
# Performance Max serves search themes, categories and retargeting out of a
# single campaign and gives back no split, so every row that arrives under
# it is the same row - and breaking it out by whatever the feed happened to
# name a row invented a "Retargeting" line that nobody bought.
COMBINED_STRATEGY = {
    "performancemaxads": "Search Theme/Category/Retargeting",
    "performancemaxadsmgmt": "Search Theme/Category/Retargeting",
}
COMBINED_KEY = "combined"


def combined_strategy(product: str | None) -> str | None:
    """The one strategy label this product reports under, if it has one."""
    entry = products.lookup(product)
    name = entry.name if entry else (product or "")
    return COMBINED_STRATEGY.get(products._key(name))


def strategy_label(product: str | None, key: str, fallback: str) -> str:
    """What to call a strategy: the product, then the targeting.

    The feed writes "FB - Home Improvement Center/Interior Design Facebook
    Premium"; the buying team writes "FB - Premium", and every sheet they
    keep is in the second form. Showing the first made the strategy tab
    unreadable and impossible to line up against their own numbers.

    Where nothing matches a known targeting the feed's own name is kept -
    a name nobody recognises is better than a wrong one that looks tidy.
    """
    combined = combined_strategy(product)
    if combined:
        return combined
    code = products.abbreviation(product)
    targeting = sheets.TARGETING_LABELS.get(key)
    if not targeting and _does_category_targeting(product):
        # An audience list with no targeting word in it is category
        # targeting: "M - Air Conditioning & Heating Mobile" is Mobile
        # Conquesting's category targeting, not a strategy of its own. The
        # feed's own name stays visible under it, so a genuinely new kind of
        # targeting shows up as something to correct rather than vanishing.
        targeting = sheets.TARGETING_LABELS["category"]
    if not targeting:
        return fallback
    return f"{code} - {targeting}" if code else targeting


# Products bought against an audience. Search and Performance Max are not:
# an unrecognised strategy on one of those is a search term or an asset
# group, and calling it a category would be wrong rather than merely vague.
NO_CATEGORY_TARGETING = {
    "payperclickads", "linkedinads", "performancemaxads",
    "performancemaxadsmgmt", "searchengineoptimization", "livechat",
    "websitevisitorid", "onlinereputationmanagement",
}


def _does_category_targeting(product: str | None) -> bool:
    entry = products.lookup(product)
    if entry is None:
        return False
    return products._key(entry.name) not in NO_CATEGORY_TARGETING


def _strategy_line_item(term: StrategyTerms, line_items: list[LineItem]) -> LineItem | None:
    """Which product a strategy runs under.

    Its own, when a buyer has said so. Otherwise the product its label names,
    matched against the products actually on this order rather than against
    every product that exists - "D - Behavioral" on an order with no Display
    line belongs to nothing, and guessing would be worse than saying so.
    """
    if term.line_item_id:
        found = next((li for li in line_items if li.id == term.line_item_id), None)
        if found is not None:
            return found

    wanted = products.product_for_strategy(term.label)
    if not wanted:
        return None
    key = products._key(wanted)
    return next(
        (li for li in line_items if products._key(li.product or "") == key), None
    )


def strategy_blocks(session, view: "OrderView") -> list[StrategyBlock]:
    """Per-strategy pacing, grouped under the product each runs in.

    Each row is computed by the same function that computes a line item's,
    against a stand-in carrying the strategy's own sold figures and its
    product's dates. Writing the arithmetic a second time here is how the
    two would come to disagree.
    """
    order = view.order
    line_items = [r.line_item for r in view.rows if r.line_item is not None]
    if not line_items:
        return []

    terms = sorted(
        session.execute(
            select(StrategyTerms).where(StrategyTerms.order_id == order.id)
        ).scalars(),
        key=lambda t: (t.sort_order, t.id),
    )


    by_product: dict[int, list[StrategyTerms]] = defaultdict(list)
    for term in terms:
        item = _strategy_line_item(term, line_items)
        if item is not None:
            by_product[item.id].append(term)

    blocks: list[StrategyBlock] = []
    for item in line_items:
        entry = products.lookup(item.product)
        block = StrategyBlock(
            line_item=item,
            label=item.product or item.name,
            code=products.abbreviation(item.product),
            hex=entry.hex if entry else "#123A63",
            text_hex=entry.text_hex if entry else "#FFFFFF",
            terms=by_product.get(item.id, []),
        )

        money = (item.pacing_type or order.pacing_type or "impression") != "impression"
        grouped = group_by_targeting(
            item.product, view.strategies.get(item.id, [])
        )
        ran = {key: series for key, _, series, _ in grouped}
        claimed: set[str] = set()
        for term in block.terms:
            key = sheets.match_key(term.label) or term.label.lower()
            series = ran.get(key)
            if series is not None:
                claimed.add(key)
            points = [
                DailyPoint(
                    date=date,
                    impressions=value if not money else 0.0,
                    cost=value if money else 0.0,
                )
                for date, value in sorted((series.by_date if series else {}).items())
            ]
            # A stand-in for the strategy: its own sold figures, its
            # product's flight and pacing type.
            ghost = LineItem(
                name=term.label,
                product=item.product,
                pacing_type=item.pacing_type,
                start_date=item.start_date,
                end_date=item.end_date,
                monthly_impressions=term.monthly_target,
                total_impressions=term.total_target,
                goal_cpm=term.rate,
                monthly_spend=term.monthly_target,
                total_spend=term.total_target,
                goal_cpc=term.rate,
            )
            row = compute_row(ghost, order, points, view.as_of)
            row.label = term.label
            row.strategy_id = term.id
            block.rows.append(row)

        for key, label, series, _ in grouped:
            if key not in claimed:
                block.unclaimed += series.total
                block.unclaimed_labels.append(label)

        running = sum(series.total for _, _, series, _ in grouped)
        rate = item.goal_cpm or item.goal_cpc or item.goal_cpe or 0.0
        # What the order says was bought, keyed on targeting so it pairs with
        # what ran however either side spells it.
        block.sold_names = sold_strategy_names(item)
        sold_keys = {
            sheets.match_key(name) or name.lower(): name
            for name in block.sold_names
        }
        block.observed = sorted(
            (
                ObservedStrategy(
                    label=label,
                    match_key=key if sheets.TARGETING_LABELS.get(key) else None,
                    delivered=series.total,
                    share=(series.total / running) if running else 0.0,
                    claimed=key in claimed,
                    sold=key in sold_keys,
                    raw_labels=raw,
                    # Impressions bought at a CPM cost that much; a spend
                    # product's delivery is already money. On a merged-CPM
                    # product - CTV + Video, Amazon Video & CTV - the rate
                    # is the half this strategy is actually set up at, not
                    # the blended one the line was bought at.
                    spend=series.total if money else (
                        series.total
                        * (ratecard.merged_strategy_cpm(item.product, label) or rate)
                        / 1000.0
                    ),
                    cpm=None if money else (
                        ratecard.merged_strategy_cpm(item.product, label) or rate
                    ),
                    days=len([v for v in series.by_date.values() if v]),
                    clicks=series.clicks,
                    conversions=series.conversions,
                    impressions=series.impressions,
                )
                for key, label, series, raw in grouped
            ),
            key=lambda o: -o.delivered,
        )

        # The product's own row, not a sum over its strategies. The goal is
        # the overall one - there is no requirement that a sold split exists,
        # and summing an empty split said every product had a goal of zero.
        block.total = next(
            (r for r in view.rows if r.line_item_id == item.id),
            total_row(block.rows, item.pacing_type or order.pacing_type or "impression"),
        )
        seen_keys = {
            sheets.match_key(o.label) or o.label.lower() for o in block.observed
        }
        block.not_running = [
            name
            for key, name in sold_keys.items()
            if key not in seen_keys
        ]
        blocks.append(block)

    return blocks


@dataclass
class DraftResult:
    orders_seen: int = 0
    orders_drafted: int = 0
    strategies_added: int = 0
    orders_without_delivery: int = 0

    def summary(self) -> str:
        parts = [
            f"{self.orders_drafted} orders given a split",
            f"{self.strategies_added} strategies drafted",
        ]
        if self.orders_without_delivery:
            parts.append(
                f"{self.orders_without_delivery} had nothing running to draft from"
            )
        return ", ".join(parts) + f" (of {self.orders_seen} missing one)"


# Orders per batch. Each one costs a handful of queries and a page's worth of
# objects, and this runs in the web worker beside whatever else it is serving.
DRAFT_BATCH = 50


def draft_missing_splits(session, as_of: dt.date | None = None) -> DraftResult:
    """Draft a sold split for every product that has none.

    The split is the only thing that says which targeting to push when a
    product is under-pacing, and nearly every order on the book is missing
    it. Doing that a product at a time, by hand, across a thousand orders is
    not work anybody is going to finish.

    What is running is knowable; what each targeting was *bought* for is not,
    so this apportions the product's sold figures by the shares actually
    running and leaves the rows editable. It is a first draft for a buyer to
    correct, not an answer - which is why it only ever fills a gap and never
    touches a row that already exists.
    """
    as_of = as_of or latest_delivery_date(session) or dt.date.today()
    result = DraftResult()

    # Only Insertion Orders that are still running: pacing a finished order
    # by strategy answers nothing anybody is going to act on.
    candidates = list(
        session.execute(
            select(Order.id)
            .where(
                func.lower(func.coalesce(Order.order_type, "")) == PACEABLE_ORDER_TYPE,
                Order.active.is_(True),
                or_(Order.end_date.is_(None), Order.end_date >= as_of),
                ~Order.id.in_(select(StrategyTerms.order_id).distinct()),
            )
            .order_by(Order.id)
        ).scalars()
    )
    result.orders_seen = len(candidates)

    for start in range(0, len(candidates), DRAFT_BATCH):
        batch = candidates[start : start + DRAFT_BATCH]
        orders = list(
            session.execute(
                select(Order)
                .where(Order.id.in_(batch))
                .options(selectinload(Order.line_items), selectinload(Order.client))
            ).scalars()
        )
        line_items = [
            li
            for order in orders
            for li in order.line_items
            if products.is_paced(li.product)
        ]
        if not line_items:
            result.orders_without_delivery += len(orders)
            continue

        # One pass over the batch rather than a full order view per order.
        # Built per order, the whole book took a hundred seconds and held a
        # worker for all of it; the shares are all this needs.
        series: dict[int, list[StrategySeries]] = {}
        for metric in ("impressions", "cost"):
            wanted = [
                li for li in line_items
                if (
                    "impressions"
                    if (li.pacing_type or li.order.pacing_type or "impression")
                    == "impression"
                    else "cost"
                ) == metric
            ]
            if wanted:
                series.update(strategy_series(session, wanted, metric=metric))

        for order in orders:
            added = 0
            for item in order.line_items:
                if not products.is_paced(item.product):
                    continue
                added += _draft_item(session, item, series.get(item.id, []))
            if added:
                result.orders_drafted += 1
                result.strategies_added += added
            else:
                result.orders_without_delivery += 1

        # Written out and let go of between batches. Holding a thousand
        # orders and their line items in one session is how the last
        # sweeping action over the whole book took the worker down.
        session.flush()
        session.expunge_all()

    return result


def _draft_item(
    session, item: LineItem, running: list["StrategySeries"]
) -> int:
    """Apportion one product's sold figures by what is running under it."""
    grouped = [
        (label, series)
        for _, label, series, _ in group_by_targeting(item.product, running)
        if series.total > 0
    ]
    total_run = sum(series.total for _, series in grouped)
    if not grouped or not total_run:
        return 0

    # What this product already has. Rows added earlier in this same run are
    # not flushed yet, so the session's pending objects are counted too -
    # without that, two products on one order running the same targeting each
    # tried to write the same row.
    taken = {
        label
        for label in session.execute(
            select(StrategyTerms.label).where(StrategyTerms.line_item_id == item.id)
        ).scalars()
    }
    if taken:
        return 0
    taken |= {
        pending.label
        for pending in session.new
        if isinstance(pending, StrategyTerms) and pending.line_item_id == item.id
    }

    monthly = (
        item.monthly_impressions or item.monthly_spend or item.client_monthly_budget
    )
    total = item.total_impressions or item.total_spend or item.client_total_budget
    rate = item.goal_cpm or item.goal_cpc or item.goal_cpe

    added = 0
    for label, entry in sorted(grouped, key=lambda pair: -pair[1].total):
        if label in taken:
            continue
        share = entry.total / total_run
        session.add(
            StrategyTerms(
                order_id=item.order_id,
                line_item_id=item.id,
                label=label,
                match_key=sheets.match_key(entry.label),
                monthly_target=(monthly * share) if monthly else None,
                total_target=(total * share) if total else None,
                rate=rate,
                sort_order=added,
                source="drafted from delivery",
                added_by_hand=True,
            )
        )
        taken.add(label)
        added += 1
    return added


def _draft_block(session, block: "StrategyBlock") -> int:
    """Apportion one product's sold figures by what is running under it."""
    if block.terms:
        return 0
    running = [o for o in block.observed if o.delivered > 0]
    if not running:
        return 0

    item = block.line_item
    monthly = (
        item.monthly_impressions or item.monthly_spend or item.client_monthly_budget
    )
    total = item.total_impressions or item.total_spend or item.client_total_budget
    rate = item.goal_cpm or item.goal_cpc or item.goal_cpe

    # What this product already has. Rows added earlier in this same run are
    # not flushed yet, so the session's pending objects are counted too -
    # without that, two products on one order running the same targeting each
    # tried to write the same row.
    taken = {
        label
        for label in session.execute(
            select(StrategyTerms.label).where(
                StrategyTerms.line_item_id == item.id
            )
        ).scalars()
    }
    taken |= {
        pending.label
        for pending in session.new
        if isinstance(pending, StrategyTerms) and pending.line_item_id == item.id
    }

    added = 0
    for observed in running:
        if observed.label in taken:
            continue
        session.add(
            StrategyTerms(
                order_id=item.order_id,
                line_item_id=item.id,
                label=observed.label,
                match_key=observed.match_key,
                monthly_target=(monthly * observed.share) if monthly else None,
                total_target=(total * observed.share) if total else None,
                rate=rate,
                sort_order=len(taken) + added,
                source="drafted from delivery",
                added_by_hand=True,
            )
        )
        taken.add(observed.label)
        added += 1
    return added
