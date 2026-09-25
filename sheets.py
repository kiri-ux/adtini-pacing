"""Parsing the buying team's hand-kept pacing sheets.

These hold one thing that exists in no export: how a line item's sold
impressions are split across its strategies. The orders drop stops at the
product line item; the delivery drop is per strategy; only these bridge the
two.

Read once, to seed. They are hand-maintained, so treating them as a live
source would have them fighting the nightly import - what is seeded here is
owned by the tool afterwards.

The layout repeats down each tab: a title row carrying the order and its run
dates, a header row naming the columns, then one row per strategy until a
blank, and a `Total:` row.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from models import PACING_CLICK, PACING_EVENT, PACING_IMPRESSION

# Which layout a section is, from the second cell of its header row.
HEADERS = {
    "impr.": PACING_IMPRESSION,
    "mon spend": PACING_CLICK,
    "client monthly budget": PACING_EVENT,
}

ORDER_NUMBER = re.compile(r"#\s*(\d{3,})")
MONEY = re.compile(r"[^0-9.\-]")


# The targeting a row is for, normalised. The sheets and the delivery feed
# name products differently ("FB/IG - Category" against "FB - Category
# Facebook"), so the two meet on the targeting rather than the whole label.
TARGETING = {
    "behavioral": "behavioral",
    "retargeting": "retargeting",
    "geo-retargeting": "geo-retargeting",
    "geo retargeting": "geo-retargeting",
    "geofencing": "geo-fencing",
    "geo-fencing": "geo-fencing",
    "geo fencing": "geo-fencing",
    "geoframing": "geo-framing",
    "geo-framing": "geo-framing",
    "ai": "ai",
    "ai targeting": "ai",
    "artificial intelligence": "ai",
    "keyword": "keyword",
    "keywords": "keyword",
    "keyword targeting": "keyword",
    "category": "category",
    "content": "content",
    "lookalike": "lookalike",
    "matching": "matching",
    "cross platform": "cross platform",
    "website retargeting": "retargeting",
    "categories": "category",
    "premium": "premium",
    "event": "event",
    "b2b": "b2b",
    "search terms": "search terms",
    "search term": "search terms",
}

# What the buying team actually types. A third of the rows in their own
# sheets are abbreviations - "SM - KW", "MC - GF", "FB - R" - and read by
# substring against the full names above they matched nothing at all, so a
# third of every strategy split went unpaired.
#
# These are matched against the targeting on its own, after the product has
# been taken off the front, because a single letter cannot be looked for
# inside a whole label without hitting the middle of a word.
ABBREVIATIONS = {
    "b": "behavioral",
    "beh": "behavioral",
    "behav": "behavioral",
    "behaviors": "behavioral",
    "behaviours": "behavioral",
    "behavior": "behavioral",
    "r": "retargeting",
    "ret": "retargeting",
    "retarg": "retargeting",
    "rt": "retargeting",
    "gr": "geo-retargeting",
    "gf": "geo-fencing",
    "geo": "geo-fencing",
    "kw": "keyword",
    "kws": "keyword",
    "cp": "cross platform",
    "cat": "category",
    "cats": "category",
    "la": "lookalike",
    "look": "lookalike",
    "prem": "premium",
    "ai": "ai",
    "b2b": "b2b",
    "st": "search terms",
}


# How each targeting is written on the page. The keys are what `match_key`
# returns; the values are the buying team's own wording.
TARGETING_LABELS = {
    "behavioral": "Behavioral",
    "retargeting": "Retargeting",
    "geo-retargeting": "Geo-Retargeting",
    "geo-fencing": "Geo-Fencing",
    "geo-framing": "Geo-Framing",
    "ai": "AI",
    "keyword": "Keyword",
    "category": "Categories",
    "content": "Content",
    "lookalike": "Lookalike",
    "matching": "Matching",
    "cross platform": "Cross Platform",
    "premium": "Premium",
    "event": "Event",
    "b2b": "B2B",
    "search terms": "Search Terms",
}


def _targeting_part(text: str) -> str:
    """The label with the product taken off the front.

    "SM - KW" is Social Mirror's keyword targeting; the part worth matching
    is "kw". Separators vary - a dash, a space, nothing at all - so the
    product is found rather than assumed.
    """
    import products

    stripped = text.strip()
    product_name = products.product_for_strategy(stripped)
    if product_name:
        for prefix in sorted(products.STRATEGY_PREFIXES, key=len, reverse=True):
            if products.STRATEGY_PREFIXES[prefix] != product_name:
                continue
            if stripped == prefix:
                return ""
            for join in (" - ", "-", " "):
                if stripped.startswith(prefix + join):
                    return stripped[len(prefix + join):].strip(" -")
    return stripped


def match_key(label: str) -> str | None:
    """The targeting a label is for, or None when it names no known one."""
    text = (label or "").lower()
    if not text.strip():
        return None

    # The full names first, longest so "geo-retargeting" is not read as
    # "retargeting". These can sit anywhere in the label, but only as whole
    # words: "ai" found inside "Air Conditioning & Heating" labelled a
    # Mobile Conquesting category audience as AI targeting, which Mobile
    # Conquesting does not even offer.
    for name in sorted(TARGETING, key=len, reverse=True):
        if re.search(r"\b" + re.escape(name) + r"\b", text):
            return TARGETING[name]

    # Then the abbreviations, against the targeting on its own.
    tail = _targeting_part(text)
    if not tail:
        return None
    if tail in ABBREVIATIONS:
        return ABBREVIATIONS[tail]

    # "SM - B2B B" is two of them; the last word is the targeting.
    words = [w for w in re.split(r"[^a-z0-9]+", tail) if w]
    for word in reversed(words):
        if word in ABBREVIATIONS:
            return ABBREVIATIONS[word]
    return None


@dataclass
class StrategyRow:
    label: str
    monthly: float | None = None
    total: float | None = None
    rate: float | None = None          # CPM, CPC or CPE

    @property
    def key(self) -> str | None:
        return match_key(self.label)


@dataclass
class Section:
    client_name: str
    title: str
    order_number: str | None
    pacing_type: str
    start_date: dt.date | None
    end_date: dt.date | None
    rows: list[StrategyRow] = field(default_factory=list)
    source: str = ""
    line: int = 0


def cells(line: str) -> list[str]:
    """One row of the markdown table the sheet is read back as."""
    return [c.strip().replace("\\", "") for c in line.split("|")][1:-1]


def number(value: str) -> float | None:
    text = (value or "").strip()
    if not text or text in {"-", "—", "$ -", "$-"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = MONEY.sub("", text)
    if text in {"", "-", "."}:
        return None
    try:
        out = float(text)
    except ValueError:
        return None
    return -out if negative else out


def date(value: str) -> dt.date | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _pacing_type(header: list[str]) -> str | None:
    if len(header) < 2:
        return None
    return HEADERS.get(header[1].strip().lower())


def parse(text: str, source: str = "") -> list[Section]:
    """Every populated section across one sheet."""
    lines = text.split("\n")
    sections: list[Section] = []

    for i, line in enumerate(lines):
        row = cells(line)
        if len(row) < 3 or "Run Dates:" not in row:
            continue

        at = row.index("Run Dates:")
        # The title sits left of "Run Dates:", occasionally split across the
        # cells before it when the client name itself contains a pipe.
        title = " ".join(c for c in row[:at] if c).strip()
        if not title:
            continue

        start = date(row[at + 1]) if len(row) > at + 1 else None
        end = date(row[at + 2]) if len(row) > at + 2 else None

        # The header row is within a couple of lines below the title.
        pacing_type = None
        header_at = None
        for j in range(i + 1, min(i + 4, len(lines))):
            found = _pacing_type(cells(lines[j]))
            if found:
                pacing_type, header_at = found, j
                break
        if not pacing_type:
            continue

        section = Section(
            client_name=title.split(" - ")[0].strip(),
            title=title,
            order_number=(ORDER_NUMBER.search(title).group(1)
                          if ORDER_NUMBER.search(title) else None),
            pacing_type=pacing_type,
            start_date=start,
            end_date=end,
            source=source,
            line=i,
        )

        for j in range(header_at + 1, len(lines)):
            body = cells(lines[j])
            if not body:
                break
            label = body[0].strip()
            if label.lower().startswith("total"):
                break
            if not label:
                # A blank label is one of the sheet's spare rows, not the end
                # of the section - the template ships a dozen of them.
                continue
            if "Run Dates:" in body:
                break
            section.rows.append(
                StrategyRow(
                    label=label,
                    monthly=number(body[1]) if len(body) > 1 else None,
                    total=number(body[2]) if len(body) > 2 else None,
                    rate=number(body[4]) if len(body) > 4 else None,
                )
            )

        if section.rows:
            sections.append(section)

    return sections
