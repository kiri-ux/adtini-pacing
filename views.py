"""Read models for the two things the buying team looks at.

* `order_view` - one order's pacing rows plus its daily grid. The per-order
  sheet, one section.
* `overview` - one line per order across every client. The summary tab.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from models import Client, DailyDelivery, LineItem, Order, StrategyTerms
import sheets
from orderbook import PACEABLE_ORDER_TYPE, PRODUCT_ABBR, line_item_label
from pacing.engine import DailyPoint, PacingRow, compute_row, health, total_row


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


def _delivery_join(line_items: list[LineItem]):
    """The condition that ties a day of delivery to the line item it ran under.

    The two exports share `order_id` and `line_item_id`, so the join is on
    ids. Delivery that carries no order id was adopted under a line item
    keyed by its own line item id, which is the second branch.
    """
    keys = {
        (li.order.external_order_id, li.external_id): li.id
        for li in line_items
        if li.external_id
    }
    return keys


def _daily_by_line_item(
    session, line_items: list[LineItem]
) -> dict[int, list[DailyPoint]]:
    """Delivery per line item per day, summed across its strategies."""
    keys = _delivery_join(line_items)
    if not keys:
        return {}

    line_item_ids = [li.external_id for li in line_items if li.external_id]
    order_ids = {li.order.external_order_id for li in line_items}

    stmt = (
        select(
            DailyDelivery.external_order_id,
            DailyDelivery.external_line_item_id,
            DailyDelivery.date,
            func.sum(DailyDelivery.impressions),
            func.sum(DailyDelivery.clicks),
            func.sum(DailyDelivery.cost),
            func.sum(DailyDelivery.conversions),
        )
        .where(DailyDelivery.external_line_item_id.in_(line_item_ids))
        .group_by(
            DailyDelivery.external_order_id,
            DailyDelivery.external_line_item_id,
            DailyDelivery.date,
        )
    )

    out: dict[int, list[DailyPoint]] = defaultdict(list)
    for order_id, li_id, date, impressions, clicks, cost, conversions in session.execute(stmt):
        target = keys.get((order_id, li_id))
        if target is None and order_id not in order_ids:
            # Adopted delivery, keyed on its line item id alone.
            target = keys.get((None, li_id))
        if target is None:
            continue
        out[target].append(
            DailyPoint(
                date=date,
                impressions=float(impressions or 0),
                clicks=float(clicks or 0),
                cost=float(cost or 0),
                conversions=float(conversions or 0),
            )
        )
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
    keys = _delivery_join(line_items)
    if not keys:
        return {}

    column = {
        "impressions": DailyDelivery.impressions,
        "clicks": DailyDelivery.clicks,
        "cost": DailyDelivery.cost,
        "conversions": DailyDelivery.conversions,
    }[metric]

    external_ids = [li.external_id for li in line_items if li.external_id]
    order_ids = {li.order.external_order_id for li in line_items}

    stmt = (
        select(
            DailyDelivery.external_order_id,
            DailyDelivery.external_line_item_id,
            DailyDelivery.strategy_id,
            DailyDelivery.strategy_name,
            DailyDelivery.strategy_type,
            DailyDelivery.product,
            DailyDelivery.date,
            func.sum(column),
        )
        .where(DailyDelivery.external_line_item_id.in_(external_ids))
        .group_by(
            DailyDelivery.external_order_id,
            DailyDelivery.external_line_item_id,
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
        order_id, li_id, strategy_id, strategy_name, strategy_type, product, date, value
    ) in session.execute(stmt):
        target = keys.get((order_id, li_id))
        if target is None and order_id not in order_ids:
            target = keys.get((None, li_id))
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
    line_items = sorted(order.line_items, key=lambda li: (li.sort_order, li.id))
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
    strategies = strategy_series(session, line_items, metric=attr)
    grid = {
        row.line_item_id: {p.date: getattr(p, attr) for p in row.daily}
        for row in rows
        if row.line_item_id is not None
    }

    return OrderView(
        order=order,
        client=order.client,
        rows=rows,
        total=total,
        as_of=as_of,
        grid_dates=grid_dates,
        grid=grid,
        on_pace_daily=total.daily_target,
        covers_from=covers_from,
        strategies=strategies,
        metric=attr,
    )


@dataclass
class ProductChip:
    """One product on an order, as the pill in the Products column.

    Carries its own served and expected so hovering the pill answers
    "how is this one doing" without opening the order.
    """

    code: str
    name: str
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
    all_line_items = [li for o in orders for li in o.line_items]
    daily = _daily_by_line_item(session, all_line_items)

    out: list[OverviewRow] = []
    for order in orders:
        line_items = sorted(order.line_items, key=lambda li: (li.sort_order, li.id))
        rows = [compute_row(li, order, daily.get(li.id, []), as_of) for li in line_items]
        total = total_row(rows, order.pacing_type)

        # One pill per product, summing the line items that share it.
        chips: dict[str, ProductChip] = {}
        for item, row in zip(line_items, rows):
            name = item.product or "Other"
            chip = chips.get(name)
            if chip is None:
                chip = ProductChip(
                    code=PRODUCT_ABBR.get(name, name[:4].upper()),
                    name=name,
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
