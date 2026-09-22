"""Reading the `orders*` drops - the sold side of the order book.

The delivery drops say what ran; these say what was sold, over what dates, at
what budget. Both land in the same S3 prefix and are told apart by filename.

The export is a flattened join and shows it. Three things have to be handled
before the numbers are usable:

* **ids arrive as HTML.** `orders_id` is
  `<a href="...viewOrder/2873">2873</a>`, not `2873`.
* **header names repeat.** `start_date` appears twice, `total_campaign_impressions`
  four times and `months_running` thirty-four times. Which copy carries the
  value varies by row, so every copy is read and the first non-null wins
  rather than one being picked by position.
* **rows repeat.** The join multiplies each line item out, so the same
  (order, line item) arrives many times over and is collapsed on load.

Spend is held per platform (`total_ppc_ad_spend`, `total_meta_ad_spend` and
so on), so the column a line item paces on depends on its product.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

# Our field -> the header names it may arrive under, most specific first.
# Every matching column is read, not just the first, because the export
# repeats names and spreads the values across the copies.
ALIASES: dict[str, tuple[str, ...]] = {
    "client_name": ("client",),
    "business_unit": ("clientbusinessunit", "ordersbusinessunit"),
    "external_order_id": ("ordersid",),
    "external_line_item_id": ("id",),
    "product": ("product",),
    "status": ("ordersstatus", "status", "ordersorderstatus"),
    "buyer": ("campaignmanager",),
    "start_date": ("ordersstartdate", "startdate"),
    "end_date": ("ordersenddate", "enddate"),
    "order_type": ("ordertype", "orderstype"),
    "notes": ("orderswhatisthegoalforthiscampaign",),

    # Impression pacing.
    "total_impressions": ("totalcampaignimpressions",),
    "monthly_impressions": ("monthlycampaignimpressions",),
    # How many months the line item runs for. Not `client_months_running`,
    # which is how long the client has been a client, nor
    # `orders_months_running`, which counts the order rather than this line.
    "months_running": ("monthsrunning",),
    "total_campaign_budget": ("totalcampaignbudget",),
    "monthly_budget": ("monthlybudget", "budgetcombined"),

    # Click and event pacing: spend is per platform.
    "total_ppc_spend": ("totalppcadspend",),
    "monthly_ppc_spend": ("monthlyppcadspend",),
    "total_linkedin_spend": ("totallinkedinadspend",),
    "monthly_linkedin_spend": ("monthlylinkedinadspend",),
    "total_pm_spend": ("totalpmadspend",),
    "monthly_pm_spend": ("monthlypmadspend",),
    "total_meta_spend": ("totalmetaadspend",),
    "monthly_meta_spend": ("monthlymetaadspend",),

    # What the client is billed, which the event sheet shows beside spend.
    "client_total_budget": ("clienttotalbudget", "totalbudgetcombined", "totalbudget"),
    "client_monthly_budget": ("clientmonthlybudget",),

    "rate_card": ("ratecard", "orderscpmtype"),
}

REQUIRED = ("client_name", "external_order_id")

DATE_FIELDS = ("start_date", "end_date")
NUMERIC_FIELDS = (
    "total_impressions", "monthly_impressions", "months_running",
    "total_campaign_budget",
    "monthly_budget", "total_ppc_spend", "monthly_ppc_spend",
    "total_linkedin_spend", "monthly_linkedin_spend", "total_pm_spend",
    "monthly_pm_spend", "total_meta_spend", "monthly_meta_spend",
    "client_total_budget", "client_monthly_budget",
)
TEXT_FIELDS = (
    "client_name", "business_unit", "external_order_id", "external_line_item_id",
    "product", "status", "buyer", "order_type", "notes", "rate_card",
)

# Which spend pair a product paces on, for the click and event sheets.
# Matched on a keyword rather than the whole name, because two vocabularies
# arrive here: the delivery feed says "PPC", the orders export says something
# longer. An exact-name map silently missed the longer one and fell back to
# the monthly budget, which is not what these pace on.
SPEND_KEYWORDS = (
    ("linkedin", ("total_linkedin_spend", "monthly_linkedin_spend")),
    ("performance max", ("total_pm_spend", "monthly_pm_spend")),
    ("pmax", ("total_pm_spend", "monthly_pm_spend")),
    ("ppc", ("total_ppc_spend", "monthly_ppc_spend")),
    ("paid search", ("total_ppc_spend", "monthly_ppc_spend")),
    ("search", ("total_ppc_spend", "monthly_ppc_spend")),
    ("meta", ("total_meta_spend", "monthly_meta_spend")),
)


def spend_columns(product: str | None) -> tuple[str, str] | None:
    """The ad-spend pair a product paces on, or None when it has none."""
    text = (product or "").strip().lower()
    if not text:
        return None
    for keyword, columns in SPEND_KEYWORDS:
        if keyword in text:
            return columns
    return None

ANCHOR = re.compile(r"<[^>]+>")
MONEY = re.compile(r"[^0-9.\-]")
# "Matt Ogden (MattOgden@vicimediainc.com)" -> "Matt Ogden"
EMAIL_SUFFIX = re.compile(r"\s*\([^)]*@[^)]*\)\s*$")


@dataclass
class OrdersFrame:
    rows: pd.DataFrame
    mapped: dict[str, list[str]] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)

    @property
    def unmapped_note(self) -> str | None:
        return ", ".join(self.unmapped) if self.unmapped else None


def simplify(name: str) -> str:
    """Reduce a header to letters and digits.

    Also drops the `.1`/`.2` suffixes pandas adds to repeated names, so every
    copy of `start_date` simplifies to the same key.
    """
    text = re.sub(r"\.\d+$", "", str(name).strip())
    return re.sub(r"[^a-z0-9]", "", text.lower())


def match_columns(columns) -> tuple[dict[str, list[str]], list[str]]:
    """Work out which source columns feed which of our fields.

    Returns every matching column per field, in header order, because the
    export spreads one field's values across its repeated columns.
    """
    by_simple: dict[str, list[str]] = {}
    for column in columns:
        by_simple.setdefault(simplify(column), []).append(column)

    mapped: dict[str, list[str]] = {}
    used: set[str] = set()

    for fieldname, aliases in ALIASES.items():
        sources: list[str] = []
        for alias in aliases:
            for column in by_simple.get(alias, []):
                if column not in used:
                    sources.append(column)
                    used.add(column)
        if sources:
            mapped[fieldname] = sources

    unmapped = [
        c for c in columns
        if c not in used and simplify(c) and not str(c).startswith("Unnamed:")
    ]
    return mapped, unmapped


def strip_html(value: object) -> str | None:
    """`<a href="...viewOrder/2873">2873</a>` is the id 2873."""
    text = _text(value)
    if text is None:
        return None
    return ANCHOR.sub("", text).strip() or None


def _text(value: object) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "<na>"}:
        return None
    # Large ids read as floats come back as "52753.0".
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _money(value: object) -> float | None:
    """`$23,814.00`, `(1,234)` and `23814` all mean a number."""
    text = _text(value)
    if text is None:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = MONEY.sub("", text)
    if text in {"", "-", "."}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def _buyer(value: object) -> str | None:
    text = _text(value)
    return EMAIL_SUFFIX.sub("", text).strip() if text else None


def _coalesce(raw: pd.DataFrame, sources: list[str]) -> pd.Series:
    """First non-null across a field's repeated columns, per row."""
    series = raw[sources[0]]
    for extra in sources[1:]:
        series = series.where(series.notna() & (series.astype(str).str.strip() != ""),
                              raw[extra])
    return series


def normalize(raw: pd.DataFrame) -> OrdersFrame:
    """Map an orders export onto our own fields, one row per line item.

    Every row is kept whatever its status. A Cancelled order may have run for
    months before it was cancelled, and that delivery still has to be
    reachable - what is worth pacing is decided further up, in `orderbook`,
    not by throwing rows away here.
    """
    mapped, unmapped = match_columns(list(raw.columns))

    missing = [f for f in REQUIRED if f not in mapped]
    if missing:
        raise ValueError(
            "orders file has no column for: " + ", ".join(missing)
            + f" (saw {len(raw.columns)} columns)"
        )

    out = pd.DataFrame(index=raw.index)
    for fieldname, sources in mapped.items():
        column = _coalesce(raw, sources)
        if fieldname in DATE_FIELDS:
            # Dates carry a time, e.g. "2018-09-01 21:00:00".
            out[fieldname] = pd.to_datetime(column, errors="coerce").dt.date
        elif fieldname in NUMERIC_FIELDS:
            out[fieldname] = column.map(_money)
        elif fieldname in ("external_order_id", "external_line_item_id"):
            out[fieldname] = column.map(strip_html)
        elif fieldname == "buyer":
            out[fieldname] = column.map(_buyer)
        else:
            out[fieldname] = column.map(_text)

    for fieldname in list(DATE_FIELDS) + list(NUMERIC_FIELDS) + list(TEXT_FIELDS):
        if fieldname not in out.columns:
            out[fieldname] = None

    # `total_campaign_impressions` does not hold a total. In the exports seen
    # it carries 0.999999999999 on every row - some ratio artifact - which
    # read straight through as a sold total of 1 and made every impression
    # figure on the page meaningless. The real total is the monthly figure
    # over the months the line item runs, which is what the hand-kept sheet
    # computes too. A value that is at least the monthly one is believable
    # and kept; anything smaller is not a total.
    #
    # Coerced to numbers first. These columns are object dtype whenever a
    # blank got turned into None, and comparing a float against a None in an
    # object column raises - so an export whose monthly column happened to be
    # empty did not merely lose its totals, it failed to load at all.
    monthly = pd.to_numeric(out["monthly_impressions"], errors="coerce")
    months = pd.to_numeric(out["months_running"], errors="coerce")
    total = pd.to_numeric(out["total_impressions"], errors="coerce")
    computed = monthly * months
    believable = total.notna() & monthly.notna() & (total >= monthly)
    out["total_impressions"] = total.where(believable, computed)

    out = out[out["client_name"].notna() & out["external_order_id"].notna()]

    # The join repeats every line item. Keep the fullest copy of each: rows
    # sorted so the ones carrying the most values land last, then keep last.
    if not out.empty:
        fullness = out.notna().sum(axis=1)
        out = (
            out.assign(_fullness=fullness)
            .sort_values("_fullness")
            .drop_duplicates(
                subset=["external_order_id", "external_line_item_id"], keep="last"
            )
            .drop(columns="_fullness")
            .reset_index(drop=True)
        )

    # Hand the order book `None` for a blank, never a float NaN.
    #
    # A numeric pandas column holds missing values as NaN, and
    # `to_dict("records")` hands that NaN straight through. NaN is not None,
    # so every "is this blank" guard downstream read it as a real number,
    # stored it, and then arithmetic on it produced NaN - which is how every
    # goal on the overview came to read "nan / nan". Converted here, once, at
    # the boundary, rather than guarded for at each of the dozen places that
    # touch one of these values.
    out = out.astype(object).where(out.notna(), None)

    return OrdersFrame(rows=out, mapped=mapped, unmapped=unmapped)
