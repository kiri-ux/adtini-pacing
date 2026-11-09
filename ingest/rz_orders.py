"""Keeping the order book current straight from RZ's order tool.

The orders exports only land on the 1st, carry ~39 copies of every line item
and miss anything created after they ran. This reads each order the same way
the order page in RZ does - one JSON call per order - so a new IO, a revised
budget or an extended flight shows up the next morning.

Which orders: every open order RZ lists, plus every Insertion Order already
in the book that ended in the last `RECENT_DAYS` days, so a late change to a
just-finished order still lands.

Rows are shaped like a normalized export row and go through the same
`import_orders` path, so locked terms, blanks-never-overwrite and the rest
behave exactly as they do for a file.

Login is the RZ bot user (RZ_USER / RZ_PASS), the same one the NOX task
worker uses. Neither the password nor any cookie value is ever logged.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from ingest.orders import EMAIL_SUFFIX, OrdersFrame

log = logging.getLogger(__name__)

BASE = "https://www.reporting.zone"
LOGIN_PATH = "/server/api/session/login"
OPEN_ORDERS_PATH = "/app/iotool/orders/openOrders"
ORDER_PATH = "/app/iotool/orders/order?id={id}"
# Every cluster (business unit). An empty list is the IO tool's "all"; set
# RZ_CLUSTER_IDS (comma separated) to narrow it.
CLUSTER_IDS: list[str] = []
# Open orders worth reading. Matched as prefixes, since RZ adds suffixes
# like "IO Live: Pending Revision". RZ's open list also carries years-old
# "RFP Complete" orders, which are skipped.
ACTIVE_STATUSES = ("io pending approval", "io pending launch", "io live", "io paused")
RECENT_DAYS = 30
# Orders written per transaction.
BATCH = 25
SOURCE_KEY = "rz://orders/{date}"

# (status, headers as (name, value) pairs, body)
Transport = Callable[[str, str, bytes | None, dict], tuple[int, list, bytes]]


class RZError(RuntimeError):
    pass


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover
        return ssl.create_default_context()


def urllib_transport(method: str, url: str, body: bytes | None, headers: dict):
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    context = _ssl_context()
    try:
        with urllib.request.urlopen(request, timeout=60, context=context) as resp:
            return resp.status, list(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, list(err.headers.items()), err.read()


class RZClient:
    """A logged-in session against RZ's internal JSON endpoints."""

    def __init__(self, user: str, password: str, transport: Transport | None = None):
        self._user = user
        self._password = password
        self._transport = transport or urllib_transport
        self._cookies: dict[str, str] = {}

    def __repr__(self) -> str:  # never show the password
        return f"RZClient(user={self._user!r})"

    # -- plumbing ----------------------------------------------------------
    def _headers(self, json_body: bool) -> dict:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE,
            "Referer": BASE + "/client/iotool/dist/",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "adtini-pacing",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        return headers

    def _keep_cookies(self, headers: list) -> None:
        # Last Set-Cookie per name wins.
        for name, value in headers:
            if name.lower() != "set-cookie":
                continue
            pair = value.split(";", 1)[0]
            if "=" in pair:
                key, val = pair.split("=", 1)
                self._cookies[key.strip()] = val.strip()

    def _send(self, method: str, path: str, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        status, headers, raw = self._transport(
            method, BASE + path, body, self._headers(payload is not None)
        )
        self._keep_cookies(headers)
        try:
            data = json.loads(raw.decode("utf-8") or "null")
        except ValueError:
            raise RZError(f"{path}: HTTP {status}, not JSON") from None
        if status >= 400:
            raise RZError(f"{path}: HTTP {status}")
        return data

    @staticmethod
    def _expired(data) -> bool:
        if not isinstance(data, dict) or data.get("status") != "redirect":
            return False
        text = json.dumps(data).lower()
        return "expired" in text or "log in" in text or "login" in text

    # -- API ---------------------------------------------------------------
    def login(self) -> None:
        self._cookies = {}
        data = self._send("POST", LOGIN_PATH, {
            "email": self._user,
            "password": self._password,
            "recaptcha": "",
            "hipaa_acknowledgement": "no",
            "two_step_verification_code": "",
            "resend_verification_code": "",
            "remember_this_device": True,
        })
        settings = ((data or {}).get("data") or {}).get("settings") or {}
        if not settings.get("isLoggedIn"):
            raise RZError("RZ login failed")

    def call(self, method: str, path: str, payload=None):
        if not self._cookies:
            self.login()
        data = self._send(method, path, payload)
        if self._expired(data):
            self.login()
            data = self._send(method, path, payload)
            if self._expired(data):
                raise RZError(f"{path}: session expired after re-login")
        return data

    def open_orders(self, cluster_ids: list[str] | None = None) -> list[dict]:
        ids = CLUSTER_IDS if cluster_ids is None else cluster_ids
        data = self.call("POST", OPEN_ORDERS_PATH, {"clusterIds": ids})
        return _unwrap_list(data)

    def order(self, order_id) -> dict:
        data = self.call("GET", ORDER_PATH.format(id=order_id))
        return _unwrap_order(data)


def _unwrap_list(data) -> list[dict]:
    while isinstance(data, dict):
        for key in ("data", "orders", "rows", "items"):
            if key in data:
                data = data[key]
                break
        else:
            break
    return data if isinstance(data, list) else []


def _unwrap_order(data) -> dict:
    for _ in range(3):
        if isinstance(data, dict) and "lineItems" in data:
            return data
        if isinstance(data, dict):
            data = data.get("order") or data.get("data")
    raise RZError("order response has no lineItems")


# ---------------------------------------------------------------------------
# JSON -> normalized rows
# ---------------------------------------------------------------------------
SPEND_KEY = re.compile(r"^(total|monthly)_(ppc|linkedin|pm|meta)_ad_spend$")
NUMBER = re.compile(r"[^0-9.\-]")


def label(value) -> str | None:
    """RZ answers are `["id", "label"]`, a list of those, or a plain value."""
    if value is None:
        return None
    if isinstance(value, list):
        if value and all(isinstance(v, list) for v in value):
            names = [label(v) for v in value]
            return ", ".join(n for n in names if n) or None
        if len(value) == 2 and not isinstance(value[1], list):
            return _clean(value[1])
        if len(value) == 1:
            return _clean(value[0])
        return None
    return _clean(value)


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def number(value) -> float | None:
    text = _clean(value)
    if text is None:
        return None
    text = NUMBER.sub("", text)
    if text in {"", "-", "."}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def date(value) -> dt.date | None:
    text = _clean(value)
    if not text or text.startswith("0000"):
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m-%d-%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def buyer(value) -> str | None:
    text = _clean(value)
    return EMAIL_SUFFIX.sub("", text).strip() or None if text else None


def sold_strategies(item: dict) -> str | None:
    """The strategies sold on a line: the product's `<prefix>_strategies`
    (or `<prefix>_strategy`, as Meta has it) answer, else the plain
    `strategies` text."""
    for key, value in item.items():
        if (key.endswith("_strategies") or key.endswith("_strategy")) \
                and isinstance(value, list) and value:
            names = label(value)
            if names:
                return names
    return _clean(item.get("strategies"))


def rows_for(order: dict, order_name: str | None = None) -> list[dict]:
    """One normalized row per line item, shaped like `ingest.orders.normalize`."""
    rows = []
    for item in order.get("lineItems") or []:
        row = {
            "client_name": label(order.get("client_id")) or _clean(order.get("client_company_name")),
            "business_unit": label(order.get("cluster_id")) or label(order.get("market_id_id")),
            "external_order_id": _clean(order.get("id")),
            "external_line_item_id": _clean(item.get("id")),
            "order_name": _clean(order_name),
            "product": _clean(item.get("product_name")),
            "status": _clean(order.get("status_name")) or label(order.get("status_id")),
            "item_status": _clean(item.get("status_name")) or _clean(item.get("line_item_status")),
            "buyer": buyer(item.get("campaign_manager_name") or order.get("campaign_manager_name")),
            "order_start_date": date(order.get("start_date")),
            "order_end_date": date(order.get("end_date")),
            "start_date": date(item.get("start_date")),
            "end_date": date(item.get("end_date")),
            "order_type": label(item.get("order_type_id")) or label(order.get("order_type_id")),
            "notes": None,
            "monthly_impressions": number(item.get("mon_impr")),
            "total_impressions": number(item.get("total_impr_goal")),
            "months_running": number(item.get("months_running")),
            "total_campaign_budget": number(item.get("total_budget")),
            "monthly_budget": number(item.get("monthly_billing_amt")),
            "client_total_budget": number(item.get("total_budget")),
            "client_monthly_budget": number(item.get("monthly_billing_amt")),
            "rate_card": label(item.get("cpm_type")) or label(order.get("cpm_type")),
            "sold_strategies": sold_strategies(item),
        }
        for key, value in item.items():
            match = SPEND_KEY.match(key)
            if match:
                row[f"{match.group(1)}_{match.group(2)}_spend"] = number(value)
        if row["client_name"] and row["external_order_id"] and row["external_line_item_id"]:
            rows.append(row)
    return rows


def frame_for(rows: list[dict]) -> OrdersFrame:
    from ingest.orders import DATE_FIELDS, NUMERIC_FIELDS, TEXT_FIELDS

    out = pd.DataFrame(rows)
    for name in list(DATE_FIELDS) + list(NUMERIC_FIELDS) + list(TEXT_FIELDS):
        if name not in out.columns:
            out[name] = None
    out = out.astype(object).where(out.notna(), None)
    return OrdersFrame(rows=out)


# ---------------------------------------------------------------------------
# The sync
# ---------------------------------------------------------------------------
@dataclass
class SyncResult:
    open_listed: int = 0
    clusters: int = 0
    orders_seen: int = 0
    orders_read: int = 0
    orders_added: int = 0
    line_items_added: int = 0
    line_items_updated: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.orders_added or self.line_items_added or self.line_items_updated)

    def summary(self) -> str:
        return (
            f"{self.open_listed} open in RZ across {self.clusters} clusters, "
            f"{self.orders_read}/{self.orders_seen} orders read, "
            f"{self.orders_added} new, {self.line_items_added} lines added, "
            f"{self.line_items_updated} updated, {len(self.errors)} errors"
        )


def recent_order_ids(session, today: dt.date, days: int = RECENT_DAYS) -> list[str]:
    """Insertion Orders in the book still marked active, or that ended in the
    last `days` days, so a flip to Complete or Cancelled lands."""
    from sqlalchemy import and_, func, or_, select

    from models import Order

    cutoff = today - dt.timedelta(days=days)
    status = func.lower(func.coalesce(Order.status, ""))
    rows = session.execute(
        select(Order.external_order_id).where(
            Order.external_order_id.isnot(None),
            func.lower(Order.order_type) == "insertion order",
            or_(
                *[status.like(f"{s}%") for s in ACTIVE_STATUSES],
                and_(Order.end_date >= cutoff, Order.end_date <= today),
            ),
        )
    ).scalars()
    return [r for r in rows if r]


def is_active(entry: dict) -> bool:
    status = (_clean(entry.get("status_name")) or "").lower()
    return status.startswith(ACTIVE_STATUSES)


def sync(
    client: RZClient,
    session_scope,
    today: dt.date | None = None,
    pause: float = 0.2,
    cluster_ids: list[str] | None = None,
) -> SyncResult:
    """Read every open and recently ended order from RZ into the order book."""
    from models import IngestedFile
    from orderbook import import_orders
    from sqlalchemy import select

    today = today or dt.date.today()
    result = SyncResult()

    names: dict[str, str | None] = {}
    listed = client.open_orders(cluster_ids)
    result.open_listed = len(listed)
    result.clusters = len({_clean(e.get("cluster_id")) for e in listed} - {None})
    if not listed:
        result.errors.append("openOrders returned no orders")
    for entry in listed:
        order_id = _clean(entry.get("id"))
        if order_id and is_active(entry):
            names[order_id] = _clean(entry.get("order_name"))
    with session_scope() as session:
        for order_id in recent_order_ids(session, today):
            names.setdefault(order_id, None)

    ids = sorted(names, key=lambda x: int(x) if x.isdigit() else 0)
    result.orders_seen = len(ids)
    log.info(
        "rz orders: %s open in RZ across %s clusters; reading %s orders",
        result.open_listed, result.clusters, len(ids),
    )

    cache: dict = {}
    for start in range(0, len(ids), BATCH):
        batch = ids[start:start + BATCH]
        rows: list[dict] = []
        for order_id in batch:
            try:
                rows.extend(rows_for(client.order(order_id), names[order_id]))
                result.orders_read += 1
            except Exception as err:  # one bad order must not stop the rest
                result.errors.append(f"order {order_id}: {err}")
            if pause:
                time.sleep(pause)
        if not rows:
            continue
        with session_scope() as session:
            cache.clear()  # objects belong to the session that loaded them
            outcome = import_orders(session, frame_for(rows), cache)
            result.orders_added += outcome.orders_added
            result.line_items_added += outcome.line_items_added
            result.line_items_updated += outcome.line_items_updated
        log.info("rz orders: %s/%s read", min(start + BATCH, len(ids)), len(ids))

    with session_scope() as session:
        key = SOURCE_KEY.format(date=today.isoformat())
        record = session.execute(
            select(IngestedFile).where(IngestedFile.s3_key == key)
        ).scalar_one_or_none() or IngestedFile(s3_key=key)
        record.kind = "orders"
        record.status = "ok" if result.orders_read else "error"
        record.message = result.summary()
        record.rows_read = result.orders_read
        record.rows_written = result.line_items_added + result.line_items_updated
        record.ingested_at = dt.datetime.utcnow()
        session.add(record)
    return result
