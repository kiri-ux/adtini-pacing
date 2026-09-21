"""Turn a raw client-serve CSV into rows the pacing engine can read.

The export is not clean: the header carries two columns both named
`goal_cpm_`, `order_id` is blank for Adlib and beta orders, and the same
(date, campaign, strategy) can appear several times when a strategy runs more
than one creative. Everything that depends on those quirks lives here.
"""
from __future__ import annotations

import datetime as dt
import re

import pandas as pd

# Source column -> our column. Anything not listed is dropped.
COLUMN_MAP = {
    "business_unit": "business_unit",
    "client": "client_name",
    "impressions": "impressions",
    "clicks": "clicks",
    "internal_cost": "cost",
    "goal_internal_cpm": "goal_cpm",
    "campaign_name": "campaign_name",
    "campaign_id": "campaign_id",
    "campaign_start_date": "campaign_start_date",
    "data_source_name": "data_source",
    "date": "date",
    "line_item_name": "line_item_name",
    "order_id": "external_order_id",
    "order_level_name": "order_level_name",
    "product": "product",
    "strategy_id": "strategy_id",
    "strategy_name": "strategy_name",
    "strategy_type": "strategy_type",
    "total_conversions": "conversions",
    "viewthroughs": "viewthroughs",
    "click_conversions": "click_conversions",
}

NUMERIC = [
    "impressions", "clicks", "cost", "conversions", "viewthroughs",
    "click_conversions", "goal_cpm",
]

# The grain we store. Duplicates within it are summed, not dropped.
GRAIN = ["date", "data_source", "campaign_id", "strategy_id"]

# Carried through on the first row of each group, for display only.
ATTRS = [
    "business_unit", "client_name", "external_order_id", "order_level_name",
    "line_item_name", "strategy_name", "strategy_type", "product",
    "campaign_name", "campaign_start_date", "goal_cpm",
]

FILENAME_DATE = re.compile(r"(\d{8})")


def file_date(key: str) -> dt.date | None:
    """The snapshot date baked into `client-serve_20260921_1202_0.csv`."""
    match = FILENAME_DATE.search(key.rsplit("/", 1)[-1])
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _clean_id(value: object) -> str:
    """Identifiers arrive as floats, with stray whitespace, or not at all.

    pandas reads a column of large numeric ids as float64, so `52753` comes
    back as `52753.0` and a 17-digit Meta id as `1.2025118369771054e+17`.
    Reading as string up front avoids the second case; this handles the rest.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "<na>"}:
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def normalize(raw: pd.DataFrame) -> pd.DataFrame:
    """Map, clean and aggregate one CSV's worth of rows."""
    # Duplicate header names become `goal_cpm_` and `goal_cpm_.1`; neither is
    # the one we want, so they are simply not in COLUMN_MAP.
    present = {src: dst for src, dst in COLUMN_MAP.items() if src in raw.columns}
    missing = set(GRAIN) - set(present.values()) - {"date"}
    if "date" not in present.values():
        raise ValueError("client-serve export has no `date` column")

    df = raw[list(present)].rename(columns=present).copy()

    for col in ("campaign_id", "strategy_id", "external_order_id"):
        if col in df.columns:
            df[col] = df[col].map(_clean_id)
        else:
            df[col] = ""

    if "data_source" not in df.columns:
        df["data_source"] = ""
    df["data_source"] = df["data_source"].fillna("").astype(str).str.strip()

    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df[df["date"].notna()]
    if "campaign_start_date" in df.columns:
        df["campaign_start_date"] = pd.to_datetime(
            df["campaign_start_date"], errors="coerce"
        ).dt.date
    else:
        df["campaign_start_date"] = None

    for col in NUMERIC:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    for col in ATTRS:
        if col not in df.columns:
            df[col] = None

    # A strategy with no id of its own still has to land somewhere distinct,
    # otherwise every unidentified strategy in a campaign collapses into one
    # row. Fall back to the strategy name, then the line item name.
    fallback = (
        df["strategy_name"].fillna("").astype(str).str.strip()
        .where(lambda s: s != "", df["line_item_name"].fillna("").astype(str).str.strip())
    )
    df["strategy_id"] = df["strategy_id"].where(
        df["strategy_id"] != "", "name:" + fallback
    )
    df["campaign_id"] = df["campaign_id"].where(
        df["campaign_id"] != "",
        "order:" + df["external_order_id"].where(
            df["external_order_id"] != "",
            df["order_level_name"].fillna("").astype(str).str.strip(),
        ),
    )

    agg = {col: "sum" for col in NUMERIC if col != "goal_cpm"}
    agg.update({col: "first" for col in ATTRS})
    out = df.groupby(GRAIN, as_index=False, dropna=False).agg(agg)

    return out
