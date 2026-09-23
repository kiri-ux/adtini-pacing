"""Read models for the two things the buying team looks at.

* `order_view` - one order's pacing rows plus its daily grid. The per-order
  sheet, one section.
* `overview` - one line per order across every client. The summary tab.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import case, false, func, or_, select, tuple_
from sqlalchemy.orm import selectinload

from models import CampaignLink, Client, DailyDelivery, LineItem, Order, StrategyTerms
import products
import sheets
from orderbook import PACEABLE_ORDER_TYPE, line_item_label
from pacing.calendar import month_bounds
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


def strategy_series(
    session, line_items: list[LineItem], metric: str = "impressions"
) -> dict[int, list[StrategySeries]]:
    """Per-strategy daily delivery, grouped under each line item."""
    join = _delivery_join(session, line_items)
    if not join:
        return {}

    column = {
        "impressions": DailyDelivery.impressions,
        "clicks": DailyDelivery.clicks,
        "cost": DailyDelivery.cost,
        "conversions": DailyDelivery.conversions,
    }[metric]

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
            func.sum(column),
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
        strategy_type, product, date, value
    ) in session.execute(stmt):
        target = join.resolve(order_id, li_id, source, campaign)
        if target is None:
            continue

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
        amount = float(value or 0)
        series.by_date[date] = series.by_date.get(date, 0.0) + amount
        series.total += amount

    for key, series in collected.items():
        series.strategy_count = len(seen_ids[key])

    out: dict[int, list[StrategySeries]] = defaultdict(list)
    for series in collected.values():
        out[series.line_item_id].append(series)
    for items in out.values():
        items.sort(key=lambda s: -s.total)
    return out


@dataclass
class PacingGroup:
    """The rows on an order that pace one particular way."""

    pacing_type: str
    rows: list[PacingRow]
    total: PacingRow

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
    covers_from: dt.date | None = None
    strategies: dict[int, list["StrategySeries"]] = field(default_factory=dict)
    metric: str = "impressions"
    daily_by_line_item: dict[int, list[DailyPoint]] = field(default_factory=dict)
    # The rows split by how they pace, order's type first. Impressions and
    # dollars need different columns and cannot share a Total, so an order
    # carrying both gets a table each rather than one table that lies about
    # one of them.
    groups: list["PacingGroup"] = field(default_factory=list)

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


def order_view(session, order_id: int, as_of: dt.date | None = None) -> OrderView | None:
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
    daily = _daily_by_line_item(session, line_items)

    rows = [compute_row(li, order, daily.get(li.id, []), as_of) for li in line_items]
    total = total_row(rows, order.pacing_type)

    # The daily grid runs the length of the flight, so a buyer can see which
    # days actually served - which is the whole point of the hand-kept sheet.
    grid_dates: list[dt.date] = []
    if order.start_date and order.end_date:
        day = max(order.start_date, covers_from) if covers_from else order.start_date
        while day <= min(order.end_date, as_of):
            grid_dates.append(day)
            day += dt.timedelta(days=1)

    attr = "impressions" if order.pacing_type == "impression" else "cost"
    # Per product, not per order: the strategies under a Pay-Per-Click line
    # are measured in spend, and asking for their impressions - which it has
    # none of - reported every one of them as having run nothing.
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
    grid = {
        row.line_item_id: {p.date: getattr(p, attr) for p in row.daily}
        for row in rows
        if row.line_item_id is not None
    }
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
        groups=groups,
        as_of=as_of,
        grid_dates=grid_dates,
        grid=grid,
        on_pace_daily=total.daily_target,
        covers_from=covers_from,
        strategies=strategies,
        metric=attr,
        daily_by_line_item=daily_by_line_item,
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
        like = f"%{query.strip()}%"
        stmt = stmt.where(Client.name.ilike(like) | Order.name.ilike(like))
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

    Cast by client name and by order id, because the two are not reliably
    both present: a campaign built before the order was written carries the
    client but no order id, which is exactly the case linking exists for.
    """
    conditions = []
    if order.client and order.client.name:
        conditions.append(DailyDelivery.client_name == order.client.name)
    if order.external_order_id:
        conditions.append(DailyDelivery.external_order_id == order.external_order_id)
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
        )
        .where(or_(*conditions))
        .group_by(DailyDelivery.data_source, DailyDelivery.campaign_id)
    )

    out: list[CampaignCandidate] = []
    for (
        source, campaign, name, product, li_id, impressions, cost, first, last,
        month_impr, month_spend,
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
        )
        by_id = join.resolve(order.external_order_id, li_id)
        if by_id is not None:
            candidate.taken_by, candidate.taken_how = by_id, "id"
        elif (source, campaign) in join.by_link:
            candidate.taken_by = join.by_link[(source, campaign)]
            candidate.taken_how = "link"
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

    @property
    def observed_total(self) -> float:
        return sum(o.delivered for o in self.observed)


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

    # What ran, per product, per targeting.
    ran: dict[int, dict[str, StrategySeries]] = defaultdict(dict)
    for line_item_id, items in view.strategies.items():
        for series in items:
            key = sheets.match_key(series.label) or series.label.lower()
            existing = ran[line_item_id].get(key)
            if existing is None:
                ran[line_item_id][key] = series
            else:
                for date, value in series.by_date.items():
                    existing.by_date[date] = existing.by_date.get(date, 0.0) + value
                existing.total += series.total

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
        claimed: set[str] = set()
        for term in block.terms:
            key = sheets.match_key(term.label) or term.label.lower()
            series = ran[item.id].get(key)
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

        for key, series in ran[item.id].items():
            if key not in claimed:
                block.unclaimed += series.total
                block.unclaimed_labels.append(series.label)

        running = sum(s.total for s in ran[item.id].values())
        block.observed = sorted(
            (
                ObservedStrategy(
                    label=series.label,
                    match_key=sheets.match_key(series.label),
                    delivered=series.total,
                    share=(series.total / running) if running else 0.0,
                    claimed=key in claimed,
                )
                for key, series in ran[item.id].items()
            ),
            key=lambda o: -o.delivered,
        )

        block.total = total_row(
            block.rows, item.pacing_type or order.pacing_type or "impression"
        )
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
    running = [s for s in running if s.total > 0]
    total_run = sum(s.total for s in running)
    if not running or not total_run:
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
    for entry in sorted(running, key=lambda s: -s.total):
        if entry.label in taken:
            continue
        share = entry.total / total_run
        session.add(
            StrategyTerms(
                order_id=item.order_id,
                line_item_id=item.id,
                label=entry.label,
                match_key=sheets.match_key(entry.label),
                monthly_target=(monthly * share) if monthly else None,
                total_target=(total * share) if total else None,
                rate=rate,
                sort_order=added,
                source="drafted from delivery",
                added_by_hand=True,
            )
        )
        taken.add(entry.label)
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
