"""The sold side of the tool.

The delivery feed says what ran; it never says what was sold, when the flight
ends, or what the client is owed. That lives here, and the buying team owns
it. `sync_from_delivery` builds the skeleton - every client, order, line item
and its link back to the feed - so the only thing left to type in is the sold
terms.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

from sqlalchemy import func, select

from models import (
    PACING_CLICK,
    PACING_EVENT,
    PACING_IMPRESSION,
    Client,
    DailyDelivery,
    DeliveryMapping,
    LineItem,
    Order,
)

log = logging.getLogger(__name__)

# Which sheet a product is paced on. Anything unlisted paces on impressions,
# which is what the buying team does today.
CLICK_PRODUCTS = {"PPC", "LinkedIn"}
EVENT_PRODUCTS = {"PMax"}

CLICK_SOURCES = {"Google Ads Search", "LinkedIn Targeting"}
EVENT_SOURCES = {"Google Ads Performance Max"}

# Short product codes, matching how the rows are labelled by hand.
PRODUCT_ABBR = {
    "Display": "D",
    "Mobile": "M",
    "Video": "V",
    "Native Display": "ND",
    "Native Video": "NV",
    "CTV": "CTV",
    "Online Audio": "OA",
    "Social Mirror": "SM",
    "Social Mirror CTV": "SM CTV",
    "Meta": "FB",
    "TikTok": "TT",
    "LinkedIn": "LI",
    "PPC": "PPC",
    "PMax": "PMax",
    "YouTube+": "YT+",
    "YouTube TV": "YTTV",
    "Amazon Premium Video": "AMZ Video",
    "Amazon Premium Display": "AMZ Display",
    "Amazon Premium CTV": "AMZ CTV",
    "Digital Out-Of-Home": "DOOH",
    "Geo-Framing": "GF",
}


def pacing_type_for(product: str | None, data_source: str | None) -> str:
    """Which of the three pacing sheets an order belongs on."""
    if product in EVENT_PRODUCTS or data_source in EVENT_SOURCES:
        return PACING_EVENT
    if product in CLICK_PRODUCTS or data_source in CLICK_SOURCES:
        return PACING_CLICK
    return PACING_IMPRESSION


def strip_client_prefix(name: str | None, client_name: str | None) -> str:
    """Drop the leading client name from a strategy or line item name.

    The feed prefixes both with the client, so "Chalfant Corporation -
    Volkswagen of Boise - Facebook/Instagram Premium Retargeting" is really
    just "Facebook/Instagram Premium Retargeting".
    """
    text = (name or "").strip()
    client = (client_name or "").strip()
    if client and text.lower().startswith(client.lower()):
        text = text[len(client) :].lstrip(" -").strip()
    return text


def line_item_label(
    product: str | None,
    strategy_type: str | None,
    strategy_name: str | None,
    client_name: str | None = None,
) -> str:
    """The "Campaign Elements" label, e.g. `SM CTV - Retargeting`."""
    code = PRODUCT_ABBR.get(product or "", product or "")
    strategy = (strategy_type or "").strip()
    if not strategy:
        # Meta, PPC and Amazon rows carry no strategy type, so the targeting
        # has to come out of the strategy name. Keep all of it after the
        # client prefix - the tail alone loses "Premium Retargeting" against
        # "Premium", and the two become one row.
        strategy = strip_client_prefix(strategy_name, client_name) or "Targeting"
    if not code:
        return strategy
    if strategy.lower().startswith(code.lower()):
        return strategy
    return f"{code} - {strategy}"


def unique_label(label: str, taken: set[str]) -> str:
    """Keep two same-named strategies apart on the same order.

    The feed genuinely ships distinct strategy ids under one name - two Meta
    ad sets both called "Facebook/Instagram Premium" - and rows a buyer cannot
    tell apart are rows they cannot pace.
    """
    if label not in taken:
        taken.add(label)
        return label
    n = 2
    while f"{label} ({n})" in taken:
        n += 1
    unique = f"{label} ({n})"
    taken.add(unique)
    return unique


def order_key(row) -> tuple[str, str]:
    """A stable identity for an order across daily drops.

    `order_id` is the real key but is blank for Adlib and beta rows, so those
    fall back to the order-level name the feed does carry.
    """
    external = (row.external_order_id or "").strip()
    if external:
        return external, (row.order_level_name or external).strip()
    name = (row.order_level_name or row.line_item_name or "").strip()
    return "", name


@dataclass
class SyncResult:
    clients_added: int = 0
    orders_added: int = 0
    line_items_added: int = 0
    mappings_added: int = 0

    def summary(self) -> str:
        return (
            f"{self.clients_added} clients, {self.orders_added} orders, "
            f"{self.line_items_added} line items, {self.mappings_added} mappings added"
        )


def sync_from_delivery(session, since: dt.date | None = None) -> SyncResult:
    """Create any client, order, line item or mapping the feed implies.

    Never edits sold terms, dates or names that already exist - a buyer's
    entry always wins over anything inferred from the feed.
    """
    result = SyncResult()

    stmt = select(
        DailyDelivery.client_name,
        DailyDelivery.external_order_id,
        DailyDelivery.order_level_name,
        DailyDelivery.line_item_name,
        DailyDelivery.data_source,
        DailyDelivery.campaign_id,
        DailyDelivery.strategy_id,
        DailyDelivery.strategy_name,
        DailyDelivery.strategy_type,
        DailyDelivery.product,
        DailyDelivery.business_unit,
        func.min(DailyDelivery.campaign_start_date).label("campaign_start_date"),
        func.avg(DailyDelivery.goal_cpm).label("goal_cpm"),
    ).group_by(
        DailyDelivery.client_name,
        DailyDelivery.external_order_id,
        DailyDelivery.order_level_name,
        DailyDelivery.line_item_name,
        DailyDelivery.data_source,
        DailyDelivery.campaign_id,
        DailyDelivery.strategy_id,
        DailyDelivery.strategy_name,
        DailyDelivery.strategy_type,
        DailyDelivery.product,
        DailyDelivery.business_unit,
    )
    if since:
        stmt = stmt.where(DailyDelivery.date >= since)

    rows = session.execute(stmt).all()

    clients = {c.name: c for c in session.execute(select(Client)).scalars()}
    orders: dict[tuple[int, str], Order] = {}
    for order in session.execute(select(Order)).scalars():
        orders[(order.client_id, order.name)] = order

    mapped = {
        (m.data_source, m.campaign_id, m.strategy_id)
        for m in session.execute(select(DeliveryMapping)).scalars()
    }
    labels_by_order: dict[int, set[str]] = {}

    for row in rows:
        client_name = (row.client_name or "").strip()
        if not client_name:
            continue

        client = clients.get(client_name)
        if client is None:
            client = Client(name=client_name, market=row.business_unit)
            session.add(client)
            session.flush()
            clients[client_name] = client
            result.clients_added += 1
        elif not client.market and row.business_unit:
            client.market = row.business_unit

        external_id, order_name = order_key(row)
        if not order_name:
            continue

        order = orders.get((client.id, order_name))
        if order is None:
            order = Order(
                client_id=client.id,
                external_order_id=external_id or None,
                name=order_name,
                pacing_type=pacing_type_for(row.product, row.data_source),
                start_date=row.campaign_start_date,
            )
            session.add(order)
            session.flush()
            orders[(client.id, order_name)] = order
            result.orders_added += 1
        elif order.start_date is None and row.campaign_start_date:
            order.start_date = row.campaign_start_date

        key = (row.data_source, row.campaign_id, row.strategy_id)
        if key in mapped:
            continue

        taken = labels_by_order.setdefault(
            order.id, {li.name for li in order.line_items}
        )
        label = unique_label(
            line_item_label(
                row.product, row.strategy_type, row.strategy_name, client_name
            ),
            taken,
        )
        line_item = LineItem(
            order_id=order.id,
            name=label,
            product=row.product,
            strategy_type=row.strategy_type,
            goal_cpm=round(row.goal_cpm, 2) if row.goal_cpm else None,
            sort_order=len(order.line_items),
        )
        session.add(line_item)
        session.flush()
        result.line_items_added += 1

        session.add(
            DeliveryMapping(
                line_item_id=line_item.id,
                data_source=row.data_source,
                campaign_id=row.campaign_id,
                strategy_id=row.strategy_id,
            )
        )
        mapped.add(key)
        result.mappings_added += 1

    session.flush()
    return result
