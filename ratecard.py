"""The rate card: what a campaign is set up at, and what it is expected to do.

Three different CPMs exist for the same line item and they are not
interchangeable:

* the **setup CPM** - what the DSP campaign is actually built at. This is the
  one the pacing sheet works in, and the one here.
* the **retail CPM** - what the client is billed. Derivable from the orders
  file as `total_campaign_budget / total_campaign_impressions`, and used for
  margin, never for pacing.
* the **partner hard cost** - what the supply partner charges. Also here,
  because margin is measured against it; the target is 50%.

Pacing a campaign against the retail rate would show a budget the buying team
never bought at.

The card is reference data, versioned in `data/rate_card.csv` so a change to
it is a reviewable commit. It moves to the database the day the team wants to
edit it in the app.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CSV_PATH = Path(__file__).parent / "data" / "rate_card.csv"

# The delivery feed's `product` -> the card's base rate name.
# PPC, LinkedIn and PMax are absent on purpose: they are bought on spend, not
# on a CPM, so there is no rate to look up.
PRODUCT_TO_RATE = {
    "Display": "Display",
    "Native Display": "Native Display",
    "Social Mirror": "Social Mirror",
    "Social Mirror CTV": "Social Mirror CTV",
    "CTV": "Connected TV",
    "Video": "Video",
    "Native Video": "Native Video",
    "Online Audio": "Online Audio",
    "Mobile": "Mobile Conquesting",
    "Meta": "Meta",
    "TikTok": "TikTok",
    "Amazon Premium Display": "Amazon Display",
    "Amazon Premium Video": "Amazon Video",
    "Amazon Premium CTV": "Amazon OTT",
    "Amazon Prime CTV": "Amazon Prime OTT",
    "YouTube+": "Youtube+",
    "YouTube TV": "YouTube TV (bids)",
    "Digital Out-Of-Home": "DOOH",
    "Geo-Framing": "Geo-Framing",
}

GOAL = re.compile(r"^\s*([\d.]+)\s*%\s*(CTR|VR)\s*$", re.I)


@dataclass(frozen=True)
class Rate:
    name: str
    starting_cpm: float | None
    max_cpm: float | None
    average_cpm: float | None
    goal_metric: str | None      # "ctr" or "vr"
    goal_value: float | None     # a rate, so 0.40% CTR is 0.004
    forecasting_cpm: float | None
    partner_hard_cost: float | None

    @property
    def goal_label(self) -> str | None:
        if not self.goal_metric or self.goal_value is None:
            return None
        return f"{self.goal_value * 100:.2f}% {self.goal_metric.upper()}"

    def margin_at(self, cpm: float | None) -> float | None:
        """Margin on a setup CPM against what the partner charges."""
        if not cpm or not self.partner_hard_cost:
            return None
        return (self.partner_hard_cost - cpm) / self.partner_hard_cost


def _float(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _goal(value: str) -> tuple[str | None, float | None]:
    match = GOAL.match(value or "")
    if not match:
        return None, None
    return match.group(2).lower(), float(match.group(1)) / 100.0


@lru_cache(maxsize=1)
def card() -> dict[str, Rate]:
    """The card, keyed by rate name."""
    out: dict[str, Rate] = {}
    if not CSV_PATH.exists():
        return out
    with CSV_PATH.open(newline="") as handle:
        for row in csv.DictReader(handle):
            metric, value = _goal(row.get("goal", ""))
            out[row["rate_name"]] = Rate(
                name=row["rate_name"],
                starting_cpm=_float(row.get("starting_cpm", "")),
                max_cpm=_float(row.get("max_cpm", "")),
                average_cpm=_float(row.get("average_cpm", "")),
                goal_metric=metric,
                goal_value=value,
                forecasting_cpm=_float(row.get("forecasting_cpm", "")),
                partner_hard_cost=_float(row.get("partner_hard_cost", "")),
            )
    return out


def rate_name(product: str | None, restricted: bool = False, b2b: bool = False) -> str | None:
    """The card entry a line item is priced from.

    Restricted and B2B categories carry their own rates, so the variant is
    looked up first and the base rate is the fallback.
    """
    base = PRODUCT_TO_RATE.get((product or "").strip())
    if base is None:
        return None
    entries = card()
    for suffix in ([" Restricted"] if restricted else []) + ([" B2B"] if b2b else []):
        if base + suffix in entries:
            return base + suffix
    return base if base in entries else None


def lookup(product: str | None, restricted: bool = False, b2b: bool = False) -> Rate | None:
    name = rate_name(product, restricted=restricted, b2b=b2b)
    return card().get(name) if name else None


def setup_cpm(product: str | None, restricted: bool = False, b2b: bool = False) -> float | None:
    """The rate to pace against when nothing more specific is known.

    The card's Max is what the sheet's own CPM column matches - a Display line
    on the hand-kept sheet reads $2.50, which is Display's Max, not its
    Starting $1.00. Starting is where a buyer opens; Max is what the campaign
    ends up set up at.
    """
    rate = lookup(product, restricted=restricted, b2b=b2b)
    if rate is None:
        return None
    return rate.max_cpm or rate.average_cpm or rate.starting_cpm
