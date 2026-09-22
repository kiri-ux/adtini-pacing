"""The sold side of the tool.

Built from the `orders*` drops. The delivery feed says what ran but never
what was sold, so the order book is what makes pacing possible at all.

Delivery joins to it on ids the two exports share - `order_id` and
`line_item_id` - so nothing has to be matched on names. Delivery that carries
no order id (Adlib and beta rows) has no sold side to join to, and
`adopt_unmatched_delivery` gives it a home so it is visible rather than
silently absent.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import func, select

import ratecard
from ingest.normalize import bound_id, name_key
from ingest.orders import SPEND_BY_PRODUCT
from models import (
    PACING_CLICK,
    PACING_EVENT,
    PACING_IMPRESSION,
    Client,
    DailyDelivery,
    LineItem,
    Order,
)

log = logging.getLogger(__name__)

# Which sheet a product is paced on. Anything unlisted paces on impressions,
# which is what most orders are.
CLICK_PRODUCTS = {"PPC", "LinkedIn"}
EVENT_PRODUCTS = {"PMax", "Performance Max"}

CLICK_SOURCES = {"Google Ads Search", "LinkedIn Targeting"}
EVENT_SOURCES = {"Google Ads Performance Max"}

# Only an Insertion Order gets a pacing page.
PACEABLE_ORDER_TYPE = "insertion order"

# Statuses that mean the order never ran. A Cancelled order is deliberately
# not in here: it may have run for months before it was cancelled, and its
# delivery still has to be visible.
NEVER_RAN_STATUSES = {"draft", "declined", "rejected", "deleted"}

# Short product codes, matching how rows are labelled by hand.
PRODUCT_ABBR = {
    "Display": "D", "Mobile": "M", "Video": "V", "Native Display": "ND",
    "Native Video": "NV", "CTV": "CTV", "Online Audio": "OA",
    "Social Mirror": "SM", "Social Mirror CTV": "SM CTV", "Meta": "FB",
    "TikTok": "TT", "LinkedIn": "LI", "PPC": "PPC", "PMax": "PMax",
    "YouTube+": "YT+", "YouTube TV": "YTTV",
    "Amazon Premium Video": "AMZ Video", "Amazon Premium Display": "AMZ Display",
    "Amazon Premium CTV": "AMZ CTV", "Digital Out-Of-Home": "DOOH",
    "Geo-Framing": "GF",
}


def pacing_type_for(product: str | None, data_source: str | None = None) -> str:
    """Which of the three pacing sheets a line item belongs on."""
    if product in EVENT_PRODUCTS or data_source in EVENT_SOURCES:
        return PACING_EVENT
    if product in CLICK_PRODUCTS or data_source in CLICK_SOURCES:
        return PACING_CLICK
    return PACING_IMPRESSION


def is_paceable(order_type: str | None) -> bool:
    """Only Insertion Orders get a pacing page."""
    return (order_type or "").strip().lower() == PACEABLE_ORDER_TYPE


def never_ran(status: str | None) -> bool:
    return (status or "").strip().lower() in NEVER_RAN_STATUSES


def strip_client_prefix(name: str | None, client_name: str | None) -> str:
    """Drop the leading client name the feed prefixes onto every name."""
    text = (name or "").strip()
    client = (client_name or "").strip()
    if client and text.lower().startswith(client.lower()):
        text = text[len(client) :].lstrip(" -").strip()
    return text


def line_item_label(
    product: str | None,
    strategy_type: str | None = None,
    strategy_name: str | None = None,
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
        strategy = strip_client_prefix(strategy_name, client_name)
    if not strategy:
        return code or "Line item"
    if not code:
        return strategy
    if strategy.lower().startswith(code.lower()):
        return strategy
    return f"{code} - {strategy}"


def unique_label(label: str, taken: set[str], external_id: str | None = None) -> str:
    """Keep two same-named rows apart on the same order.

    Two line items of the same product are the normal case, and they differ
    by their line item id - so that is what distinguishes them, rather than a
    counter that says only "this is the second one".
    """
    if label not in taken:
        taken.add(label)
        return label

    if external_id and not str(external_id).startswith("name:"):
        candidate = f"{label} · {external_id}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate

    n = 2
    while f"{label} ({n})" in taken:
        n += 1
    unique = f"{label} ({n})"
    taken.add(unique)
    return unique


def retail_cpm(total_budget: float | None, total_impressions: float | None) -> float | None:
    """What the client is billed per thousand, from the orders file.

    This is the retail rate, not the rate the campaign was set up at, so it
    is for margin - never for pacing.
    """
    if not total_budget or not total_impressions:
        return None
    return round(total_budget / total_impressions * 1000, 2)


def resolve_goal_cpm(
    product: str | None, restricted: bool, total_budget: float | None,
    total_impressions: float | None,
) -> tuple[float | None, str | None]:
    """The CPM to pace against, and where it came from.

    The rate card wins: it holds what the DSP campaign is actually set up at,
    which is what the buying team's sheet works in. The orders file's budget
    over its impressions is the retail rate the client pays, which is higher
    and would pace the line against a budget nobody bought at - it is only
    the fallback for a product the card does not price.
    """
    from_card = ratecard.setup_cpm(product, restricted=restricted)
    if from_card:
        return from_card, "rate card"
    derived = retail_cpm(total_budget, total_impressions)
    if derived:
        return derived, "orders file"
    return None, None


@dataclass
class ImportResult:
    clients_added: int = 0
    orders_added: int = 0
    orders_updated: int = 0
    line_items_added: int = 0
    line_items_updated: int = 0
    locked_skipped: int = 0

    def summary(self) -> str:
        parts = [
            f"{self.orders_added} orders added",
            f"{self.orders_updated} updated",
            f"{self.line_items_added} line items added",
            f"{self.line_items_updated} updated",
        ]
        if self.locked_skipped:
            parts.append(f"{self.locked_skipped} left alone (edited by hand)")
        return ", ".join(parts)


def _keep(item: LineItem, field: str, value) -> None:
    """Set a sold term, unless that would replace a figure with a blank.

    The export repeats a line item across its rows and chunks, and the
    repeats are not equally complete. Letting a sparser copy win would empty
    terms that a fuller copy had already supplied.
    """
    if value is None and getattr(item, field) is not None:
        return
    setattr(item, field, value)


def _apply_sold_terms(item: LineItem, row, pacing_type: str) -> None:
    """Copy the sold terms for the sheet this line item paces on."""
    if pacing_type == PACING_IMPRESSION:
        _keep(item, "total_impressions", row.get("total_impressions"))
        _keep(item, "monthly_impressions", row.get("monthly_impressions"))
        item.goal_cpm, item.goal_cpm_source = resolve_goal_cpm(
            row.get("product"),
            bool(item.restricted),
            row.get("total_campaign_budget"),
            row.get("total_impressions"),
        )
        return

    total_key, monthly_key = SPEND_BY_PRODUCT.get(
        row.get("product") or "", ("total_campaign_budget", "monthly_budget")
    )
    total = row.get(total_key)
    monthly = row.get(monthly_key)

    if pacing_type == PACING_CLICK:
        _keep(item, "total_spend", total)
        _keep(item, "monthly_spend", monthly)
        # The orders file prices clicks by budget, not by a CPC, so the goal
        # rate is left for a buyer to set where they want one.
        return

    _keep(item, "google_total_spend", total)
    _keep(item, "google_monthly_spend", monthly)
    _keep(item, "client_total_budget", row.get("client_total_budget"))
    _keep(item, "client_monthly_budget", row.get("client_monthly_budget"))


def import_orders(session, frame) -> ImportResult:
    """Upsert an orders export into the order book.

    Anything a buyer has marked `terms_locked` is left exactly as it is -
    budgets get adjusted mid-flight and those adjustments must survive the
    next import.
    """
    result = ImportResult()

    clients = {c.name: c for c in session.execute(select(Client)).scalars()}
    orders = {
        o.external_order_id: o
        for o in session.execute(
            select(Order).where(Order.external_order_id.isnot(None))
        ).scalars()
    }

    for row in frame.rows.to_dict("records"):
        client_name = row.get("client_name")
        external_order_id = row.get("external_order_id")
        if not client_name or not external_order_id:
            continue

        client = clients.get(client_name)
        if client is None:
            client = Client(name=client_name, market=row.get("business_unit"))
            session.add(client)
            session.flush()
            clients[client_name] = client
            result.clients_added += 1
        elif not client.market and row.get("business_unit"):
            client.market = row.get("business_unit")

        product = row.get("product")
        pacing_type = pacing_type_for(product)

        order = orders.get(external_order_id)
        if order is None:
            order = Order(
                client_id=client.id,
                external_order_id=external_order_id,
                name=row.get("order_name") or f"{client_name} #{external_order_id}",
                pacing_type=pacing_type,
            )
            session.add(order)
            session.flush()
            orders[external_order_id] = order
            result.orders_added += 1
        else:
            result.orders_updated += 1

        # Status and type always come from the file - they are the file's to
        # say, and the pacing rules key off them.
        order.order_type = row.get("order_type")
        order.status = row.get("status")
        order.active = not never_ran(row.get("status"))
        if row.get("buyer") and not order.buyer:
            order.buyer = row.get("buyer")

        if not order.terms_locked:
            order.start_date = row.get("start_date") or order.start_date
            order.end_date = row.get("end_date") or order.end_date
            order.pacing_type = pacing_type
        elif order.terms_locked:
            result.locked_skipped += 1

        external_line_item_id = row.get("external_line_item_id")
        item = next(
            (
                li for li in order.line_items
                if li.external_id and li.external_id == external_line_item_id
            ),
            None,
        )
        if item is None:
            taken = {li.name for li in order.line_items}
            item = LineItem(
                order_id=order.id,
                external_id=external_line_item_id,
                name=unique_label(
                    line_item_label(product), taken, external_line_item_id
                ),
                product=product,
                sort_order=len(order.line_items),
            )
            session.add(item)
            order.line_items.append(item)
            result.line_items_added += 1
        else:
            result.line_items_updated += 1

        item.product = product or item.product
        if item.terms_locked:
            result.locked_skipped += 1
        else:
            item.start_date = row.get("start_date")
            item.end_date = row.get("end_date")
            _apply_sold_terms(item, row, pacing_type)

    session.flush()
    return result


@dataclass
class AdoptResult:
    orders_added: int = 0
    line_items_added: int = 0

    def summary(self) -> str:
        return (
            f"{self.orders_added} orders and {self.line_items_added} line items "
            "created for delivery with no order record"
        )


def adopt_unmatched_delivery(session) -> AdoptResult:
    """Give delivery that no order row explains somewhere to show up.

    Adlib and beta rows carry no order id, so there is nothing to join them
    to. Rather than have that delivery vanish, an order is created from the
    names the feed does carry. These have no sold terms and pace as
    "needs terms" until someone fills them in.
    """
    result = AdoptResult()

    known_orders = {
        o.external_order_id
        for o in session.execute(
            select(Order).where(Order.external_order_id.isnot(None))
        ).scalars()
    }

    rows = session.execute(
        select(
            DailyDelivery.client_name,
            DailyDelivery.external_order_id,
            DailyDelivery.external_line_item_id,
            DailyDelivery.order_level_name,
            DailyDelivery.line_item_name,
            DailyDelivery.product,
            DailyDelivery.restricted,
            DailyDelivery.business_unit,
            func.min(DailyDelivery.campaign_start_date).label("start_date"),
        ).group_by(
            DailyDelivery.client_name,
            DailyDelivery.external_order_id,
            DailyDelivery.external_line_item_id,
            DailyDelivery.order_level_name,
            DailyDelivery.line_item_name,
            DailyDelivery.product,
            DailyDelivery.restricted,
            DailyDelivery.business_unit,
        )
    ).all()

    clients = {c.name: c for c in session.execute(select(Client)).scalars()}
    adopted: dict[tuple[int, str], Order] = {}
    for order in session.execute(
        select(Order).where(Order.external_order_id.is_(None))
    ).scalars():
        adopted[(order.client_id, order.name)] = order

    for row in rows:
        client_name = (row.client_name or "").strip()
        if not client_name:
            continue
        # Delivery whose order is already on the books joins by id; nothing
        # to adopt.
        if (row.external_order_id or "") in known_orders and row.external_order_id:
            continue

        client = clients.get(client_name)
        if client is None:
            client = Client(name=client_name, market=row.business_unit)
            session.add(client)
            session.flush()
            clients[client_name] = client

        name = (row.order_level_name or row.line_item_name or "").strip()
        if not name:
            continue

        order = adopted.get((client.id, name))
        if order is None:
            order = Order(
                client_id=client.id,
                external_order_id=None,
                name=name,
                pacing_type=pacing_type_for(row.product),
                start_date=row.start_date,
                order_type="Insertion Order",
                status="Unmatched",
            )
            session.add(order)
            session.flush()
            adopted[(client.id, name)] = order
            result.orders_added += 1

        # The delivery loader builds the same key for a line item the feed
        # gave no id, so the two sides meet.
        key = row.external_line_item_id or bound_id(name_key("name", name))
        if any(li.external_id == key for li in order.line_items):
            continue

        taken = {li.name for li in order.line_items}
        restricted = (row.restricted or "").strip().lower() == "yes"
        cpm, source = resolve_goal_cpm(row.product, restricted, None, None)
        item = LineItem(
            order_id=order.id,
            external_id=key,
            name=unique_label(line_item_label(row.product), taken, key),
            product=row.product,
            restricted=restricted,
            goal_cpm=cpm,
            goal_cpm_source=source,
            sort_order=len(order.line_items),
        )
        session.add(item)
        # Appended so the next row of the same order sees it when checking
        # for duplicates, before the flush.
        order.line_items.append(item)
        result.line_items_added += 1

    session.flush()
    return result
