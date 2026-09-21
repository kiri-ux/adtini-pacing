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

from models import Client, DailyDelivery, DeliveryMapping, LineItem, Order
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


def _daily_by_line_item(
    session, line_item_ids: list[int]
) -> dict[int, list[DailyPoint]]:
    """Delivery per line item per day, summed across its mapped strategies."""
    if not line_item_ids:
        return {}

    stmt = (
        select(
            DeliveryMapping.line_item_id,
            DailyDelivery.date,
            func.sum(DailyDelivery.impressions),
            func.sum(DailyDelivery.clicks),
            func.sum(DailyDelivery.cost),
            func.sum(DailyDelivery.conversions),
        )
        .join(
            DailyDelivery,
            (DailyDelivery.data_source == DeliveryMapping.data_source)
            & (DailyDelivery.campaign_id == DeliveryMapping.campaign_id)
            & (DailyDelivery.strategy_id == DeliveryMapping.strategy_id),
        )
        .where(DeliveryMapping.line_item_id.in_(line_item_ids))
        .group_by(DeliveryMapping.line_item_id, DailyDelivery.date)
    )

    out: dict[int, list[DailyPoint]] = defaultdict(list)
    for li_id, date, impressions, clicks, cost, conversions in session.execute(stmt):
        out[li_id].append(
            DailyPoint(
                date=date,
                impressions=float(impressions or 0),
                clicks=float(clicks or 0),
                cost=float(cost or 0),
                conversions=float(conversions or 0),
            )
        )
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
    daily = _daily_by_line_item(session, [li.id for li in line_items])

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
    )


@dataclass
class OverviewRow:
    order: Order
    client: Client
    total: PacingRow
    as_of: dt.date
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
) -> list[OverviewRow]:
    """One computed line per order, which is the summary tab."""
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
    if not include_ended:
        stmt = stmt.where((Order.end_date.is_(None)) | (Order.end_date >= as_of))

    orders = list(session.execute(stmt).scalars())
    all_line_items = [li for o in orders for li in o.line_items]
    daily = _daily_by_line_item(session, [li.id for li in all_line_items])

    out: list[OverviewRow] = []
    for order in orders:
        rows = [
            compute_row(li, order, daily.get(li.id, []), as_of)
            for li in sorted(order.line_items, key=lambda li: (li.sort_order, li.id))
        ]
        out.append(
            OverviewRow(
                order=order,
                client=order.client,
                total=total_row(rows, order.pacing_type),
                as_of=as_of,
                covers_from=covers_from,
            )
        )

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
