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
import math
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

import products
import ratecard
from ingest.normalize import bound_id, name_key
from ingest.orders import spend_columns
from pacing.calendar import months_between
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
# Normalised product names, because the two exports do not use the same
# ones: the delivery feed says "PPC" and "PMax" where the orders file says
# "Pay-Per-Click Ads" and "Performance Max Ads". Matching only the feed's
# spelling meant every one of these imported from an orders file was paced
# on impressions - which a Pay-Per-Click line does not have and was never
# sold any - so its sold spend was never even read.
CLICK_PRODUCTS = {"ppc", "linkedin", "payperclickads", "linkedinads"}
EVENT_PRODUCTS = {"pmax", "performancemax", "performancemaxads"}

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
    # As the orders export names them.
    "Display Ads": "D",
    "Native Display Ads": "ND",
    "Social Mirror Ads": "SM",
    "Social Mirror CTV Ads": "SM CTV",
    "Native Video Ads": "NV",
    "Video Ads": "V",
    "Connected TV Ads": "CTV",
    "CTV + Video Ads": "CTV+V",
    "Online Audio Ads": "OA",
    "Mobile Conquesting Display & Video Ads": "MC",
    "Mobile Conquesting Event/Political Display & Video Ads": "MC EV",
    "Meta Display & Video Ads": "META",
    "Meta Lead Display & Video Ads": "META LEAD",
    "Amazon Premium Display Ads": "AMZ D",
    "Amazon Premium Video Ads": "AMZ V",
    "Amazon Premium CTV Ads": "AMZ CTV",
    "Youtube+ Video Ads": "YT+",
    "YouTube TV Video Ads (bids)": "YTTV",
    "TikTok Display & Video Ads": "TT",
    "Digital Out-Of-Home (DOOH) Display & Video Ads": "DOOH",
    "Dynamic Display Ads": "DYN",
    "Geo-Framing Display Ads": "GF",
    # As the delivery feed names them.
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
    """Which of the three pacing sheets a line item belongs on.

    The name is canonicalised first, so the feed's spelling and the orders
    file's both land on the same answer.
    """
    entry = products.lookup(product)
    key = products._key((entry.name if entry else product) or "")
    if key in EVENT_PRODUCTS or data_source in EVENT_SOURCES:
        return PACING_EVENT
    if key in CLICK_PRODUCTS or data_source in CLICK_SOURCES:
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

    A pandas NaN counts as a blank. The parse converts them before they get
    here, but this is the function whose job is to recognise a missing value,
    and `value is None` alone does not: a NaN sailed through, was stored, and
    turned every goal on the overview into "nan".
    """
    if isinstance(value, float) and math.isnan(value):
        value = None
    # Zero is not a sold term either. Nothing is sold at zero impressions or
    # zero budget, so a zero is a blank the export wrote as a number - and
    # letting one win replaced a real figure from a fuller export with a
    # goal of nothing. The page already reads 0 as unset when a buyer types
    # it, so this only makes the import agree.
    if value == 0:
        value = None
    current = getattr(item, field)
    if isinstance(current, float) and math.isnan(current):
        # A NaN already on the row is damage, not a figure worth protecting.
        current = None
    if value is None and current is not None:
        return
    setattr(item, field, value)


def _fix_impossible_total(item: LineItem, months) -> None:
    """A total below the monthly figure is not a total.

    Several orders exports describe the same line item and they do not agree:
    `total_campaign_impressions` is a ratio artifact in some, a month count in
    others, a real total in the rest. Each file's own parse rejects a total it
    can see is wrong, but a file only sees its own rows - whichever export
    lands last wins at the point of storage, and one carrying a month count
    put a sold total of 34 against a monthly goal of 6.5 million. Total pacing
    then read 1,909,747%.

    So the rule is applied again here, where every file meets: a total smaller
    than one month of it is the wrong number whatever supplied it, and the
    months the line item runs are what say what the total should be. With no
    month count to rebuild from, the total is cleared - "not known yet" reads
    as a dash and is obviously unset, where a wrong number reads as a fact.
    """
    monthly = item.monthly_impressions
    total = item.total_impressions
    if monthly is None or total is None or not monthly or total >= monthly:
        return
    try:
        months = float(months) if months is not None else None
    except (TypeError, ValueError):
        months = None
    if months is not None and (math.isnan(months) or months <= 0):
        months = None
    if months is None:
        # The export did not say, so the flight says instead: a line item
        # selling a monthly figure over six months has sold six of them.
        months = _flight_months(item)
    item.total_impressions = monthly * months if months else None


def _flight_months(item: LineItem) -> int | None:
    """How many months the line item runs, from its dates.

    Its own dates when it has them, otherwise the order's - a line item
    without dates runs for the order's flight.
    """
    order = item.order
    start = item.start_date or (order.start_date if order else None)
    end = item.end_date or (order.end_date if order else None)
    if start is None or end is None:
        return None
    months = months_between(start, end)
    return months or None


def _apply_sold_terms(item: LineItem, row, pacing_type: str) -> None:
    """Copy the sold terms for the sheet this line item paces on."""
    if pacing_type == PACING_IMPRESSION:
        _keep(item, "monthly_impressions", row.get("monthly_impressions"))
        _keep(item, "total_impressions", row.get("total_impressions"))
        _fix_impossible_total(item, row.get("months_running"))
        item.goal_cpm, item.goal_cpm_source = resolve_goal_cpm(
            row.get("product"),
            bool(item.restricted),
            row.get("total_campaign_budget"),
            row.get("total_impressions"),
        )
        return

    columns = spend_columns(row.get("product"))

    if pacing_type == PACING_CLICK:
        # PPC and LinkedIn pace on the ad spend, not on the client's monthly
        # budget - the budget includes the management fee, which never
        # reaches the platform.
        if columns is None:
            return
        _keep(item, "total_spend", row.get(columns[0]))
        _keep(item, "monthly_spend", row.get(columns[1]))
        # The orders file prices clicks by budget, not by a CPC, so the goal
        # rate is left for a buyer to set where they want one.
        return

    # Performance Max paces the client's budget against the client's cost.
    # The platform spend is kept beside it, because the ratio between the two
    # is what turns a delivered platform cost into a client-facing one.
    _keep(item, "client_total_budget", row.get("client_total_budget"))
    _keep(item, "client_monthly_budget", row.get("client_monthly_budget"))
    if columns is not None:
        _keep(item, "google_total_spend", row.get(columns[0]))
        _keep(item, "google_monthly_spend", row.get(columns[1]))


def import_orders(session, frame, cache: dict | None = None) -> ImportResult:
    """Upsert an orders export into the order book.

    Anything a buyer has marked `terms_locked` is left exactly as it is -
    budgets get adjusted mid-flight and those adjustments must survive the
    next import.

    `cache` holds the client and order lookups across the chunks of one file.
    Rebuilding them per chunk meant a 600MB export re-read the whole order
    book thirty times over and held every row it touched.
    """
    result = ImportResult()

    if cache is None:
        cache = {}
    if "clients" not in cache:
        cache["clients"] = {
            c.name: c for c in session.execute(select(Client)).scalars()
        }
        cache["orders"] = {
            o.external_order_id: o
            for o in session.execute(
                select(Order).where(Order.external_order_id.isnot(None))
            ).scalars()
        }
    clients = cache["clients"]
    orders = cache["orders"]

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
            # The order's type is whatever its first line item paces on.
            # It used to be whatever the *last* row happened to be, so an
            # order carrying Display and Pay-Per-Click got one or the other
            # depending on the order the export listed them in, and half its
            # lines were then paced on the wrong thing entirely.
            if not order.pacing_type:
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
        if row.get("sold_strategies"):
            item.sold_strategies = row.get("sold_strategies")
        # A product that paces differently from its order says so on itself.
        item.pacing_type = pacing_type if pacing_type != order.pacing_type else None
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
    adopted: dict[tuple[int, str], Order] = {
        (order.client_id, order.name): order
        for order in session.execute(select(Order)).scalars()
    }

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
                # Kept, not discarded. Delivery often carries an order id that
                # simply has no orders row yet; throwing it away meant the
                # orders file later created a second order for the same thing,
                # and nothing else could match on it either.
                external_order_id=(row.external_order_id or "").strip() or None,
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


@dataclass
class RecomputeResult:
    line_items: int = 0
    cpm_set: int = 0
    pacing_fixed: int = 0
    totals_rebuilt: int = 0
    totals_cleared: int = 0
    locked_skipped: int = 0

    def summary(self) -> str:
        parts = [
            f"{self.line_items} line items checked",
            f"{self.cpm_set} CPMs set from the rate card",
            f"{self.pacing_fixed} paced on the right thing",
            f"{self.totals_rebuilt} totals rebuilt",
        ]
        if self.totals_cleared:
            parts.append(f"{self.totals_cleared} cleared as unusable")
        if self.locked_skipped:
            parts.append(f"{self.locked_skipped} left alone (edited by hand)")
        return ", ".join(parts)


# Line items per batch. The worker has 512MB and shares it with whatever
# else is being served, so the book is walked a piece at a time.
RECOMPUTE_BATCH = 1000


def recompute_terms(session) -> RecomputeResult:
    """Re-derive what is computed rather than imported, without any file.

    Two of the sold terms are not taken from the export, they are worked out
    from it: the setup CPM comes from the rate card, and the total is the
    monthly figure over the months the line item runs. Both have been wrong
    in the stored data - the rate card was not deployed at all for a while,
    and several parser faults put a ratio artifact where the total belongs.

    Fixing the code only fixes what is imported next, and a sweep skips a
    file it has already read, so the damaged rows would sit there until every
    orders export - gigabytes of them - was read again for two columns that
    do not come from the file in the first place. This recomputes them where
    they stand.

    Anything a buyer has edited by hand is left alone, as everywhere else.
    """
    result = RecomputeResult()
    last_id = 0

    while True:
        batch = list(
            session.execute(
                select(LineItem)
                .options(joinedload(LineItem.order))
                .where(LineItem.id > last_id)
                .order_by(LineItem.id)
                .limit(RECOMPUTE_BATCH)
            ).scalars()
        )
        if not batch:
            break

        for item in batch:
            result.line_items += 1
            if item.terms_locked:
                result.locked_skipped += 1
                continue

            # How a product paces is worked out from the product, not taken
            # from the file - and it was wrong for every Pay-Per-Click,
            # LinkedIn and Performance Max line stored before the two
            # exports' product names were matched properly. Left wrong, a
            # spend line reads as impression paced and its cost comes out as
            # impressions times a CPM it does not have, which is zero.
            kind = pacing_type_for(item.product)
            order_kind = item.order.pacing_type if item.order else None
            wanted = kind if kind != order_kind else None
            if wanted != item.pacing_type:
                item.pacing_type = wanted
                result.pacing_fixed += 1

            cpm, source = resolve_goal_cpm(
                item.product, bool(item.restricted), None, item.total_impressions
            )
            if cpm and cpm != item.goal_cpm:
                item.goal_cpm, item.goal_cpm_source = cpm, source
                result.cpm_set += 1

            before = item.total_impressions
            _fix_impossible_total(item, months=None)
            if item.total_impressions != before:
                if item.total_impressions is None:
                    result.totals_cleared += 1
                else:
                    result.totals_rebuilt += 1

        last_id = batch[-1].id
        # Written out and let go of. Holding every line item on the book and
        # every order behind it in one session is what took the worker down:
        # the page came back a 502 and the service with it.
        session.flush()
        session.expunge_all()

    return result
