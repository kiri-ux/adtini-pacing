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

# Product -> the card's base rate name. Two vocabularies arrive here and both
# have to resolve: the delivery feed uses short names ("Meta", "Display"),
# while the orders export uses the rate sheet's own ("Meta Display & Video
# Ads"). Mapping only the first left every order-sourced line item without a
# CPM, which is what emptied the budget columns.
# PPC, LinkedIn and PMax are absent on purpose: they are bought on spend, not
# on a CPM, so there is no rate to look up.
PRODUCT_TO_RATE = {
    # --- as the orders export names them -------------------------------
    "Display Ads": "Display",
    "Native Display Ads": "Native Display",
    "Social Mirror Ads": "Social Mirror",
    "Native Video Ads": "Native Video",
    "Video Ads": "Video",
    "Connected TV Ads": "Connected TV",
    "CTV + Video Ads": "CTV + Video",
    "Social Mirror CTV Ads": "Social Mirror CTV",
    "Online Audio Ads": "Online Audio",
    "Mobile Conquesting Display & Video Ads": "Mobile Conquesting",
    "Mobile Conquesting Event/Political Display & Video Ads":
        "Mobile Conquesting Event",
    "Mobile Conquesting EVENT or POLITICAL CATEGORY TARGETING Display & Video Ads":
        "Mobile Conquesting Event",
    "Meta Display & Video Ads": "Meta",
    "Meta Lead Display & Video Ads": "Meta Lead",
    "Amazon Premium Display Ads": "Amazon Display",
    "Amazon Premium Video Ads": "Amazon Video",
    "Amazon Premium CTV Ads": "Amazon OTT",
    "Amazon Prime CTV Ads": "Amazon Prime OTT",
    "Amazon Premium CTV + Video Ads": "Amazon OTT",
    # Video and OTT sold together. Priced off Video, which is the cheaper of
    # the two rates - a setup CPM guessed high reads a campaign as spending
    # more than it did.
    "Amazon Premium Video (with Twitch) & OTT Ads": "Amazon Video",
    "Youtube+ Video Ads": "Youtube+",
    "YouTube Video Ads": "Youtube+",
    "YouTube TV Video Ads (bids)": "YouTube TV (bids)",
    "YouTube TV Video Ads (actuals)": "YouTube TV (actual YT TV CPM)",
    "TikTok Display & Video Ads": "TikTok",
    "Digital Out-Of-Home (DOOH) Display & Video Ads": "DOOH",
    "Dynamic Display Ads": "Dynamic",
    "Dynamic Ads": "Dynamic",
    "Geo-Framing Display Ads": "Geo-Framing",

    # --- as the delivery feed names them -------------------------------
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

# Products sold at one blended CPM that are set up as two. The line is
# bought as a single product, but its video lines run at the video rate and
# its CTV lines at the CTV rate - so costing every strategy under it at one
# number is wrong for both halves.
#
# Keyed by the card entry the product resolves to, and read at the strategy
# level, where which half a row is is knowable. The line item itself keeps
# its single card rate, because that is what the budget was struck at.
MERGED_RATES: dict[str, dict[str, str]] = {
    "CTV + Video": {"video": "Video", "ctv": "Connected TV"},
    "Amazon Video": {"video": "Amazon Video", "ctv": "Amazon OTT"},
    "Amazon OTT": {"video": "Amazon Video", "ctv": "Amazon OTT"},
}

# What a strategy name has to carry to be one half or the other. CTV first:
# "CTV + Video" contains both words and is the CTV half of nothing - it is
# the product, not a strategy - but a strategy named "OTT Video" is CTV.
CTV_WORDS = ("ctv", "ott", "connected tv", "streaming")
VIDEO_WORDS = ("video", "pre-roll", "preroll", "instream", "in-stream")

GOAL = re.compile(r"^\s*([\d.]+)\s*%\s*(CTR|VR)\s*$", re.I)


@dataclass(frozen=True)
class Rate:
    name: str
    starting_cpm: float | None
    max_cpm: float | None
    # What a campaign is actually set up at. The card's own column, and the
    # one the buying team budgets from - Max is the ceiling, not the plan.
    al_starting_max_cpm: float | None
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
                al_starting_max_cpm=_float(row.get("al_starting_max_cpm", "")),
                average_cpm=_float(row.get("average_cpm", "")),
                goal_metric=metric,
                goal_value=value,
                forecasting_cpm=_float(row.get("forecasting_cpm", "")),
                partner_hard_cost=_float(row.get("partner_hard_cost", "")),
            )
    return out


@lru_cache(maxsize=1)
def _by_key() -> dict[str, str]:
    """`PRODUCT_TO_RATE`, keyed so punctuation and case cannot miss it."""
    import products as _products

    return {_products._key(name): rate for name, rate in PRODUCT_TO_RATE.items()}


def rate_name(product: str | None, restricted: bool = False, b2b: bool = False) -> str | None:
    """The card entry a line item is priced from.

    Restricted and B2B categories carry their own rates, so the variant is
    looked up first and the base rate is the fallback.

    The name is canonicalised through the product registry first. Matched on
    the raw string, four live products missed the card entirely - Dynamic,
    YouTube, Amazon Video and Mobile Conquesting's event rate - and every
    line of them fell through to the client's retail CPM.
    """
    import products as _products

    entry = _products.lookup(product)
    key = _products._key((entry.name if entry else product) or "")
    base = _by_key().get(key)
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


def merged_halves(product: str | None) -> dict[str, str] | None:
    """The two rates a merged-CPM product is actually set up at, or None."""
    base = rate_name(product)
    return MERGED_RATES.get(base) if base else None


def merged_strategy_cpm(product: str | None, strategy: str | None) -> float | None:
    """The half of a merged-CPM product this strategy is set up at.

    None for everything else, and for a strategy on a merged product that
    names neither half - the caller then keeps the line item's own rate,
    which may be one a buyer typed and is not the card's to overrule.
    """
    halves = merged_halves(product)
    if not halves:
        return None
    text = (strategy or "").lower()
    # CTV wins a name carrying both, because a CTV line described as video
    # is still bought as CTV.
    for half, words in (("ctv", CTV_WORDS), ("video", VIDEO_WORDS)):
        if any(word in text for word in words):
            entry = card().get(halves[half])
            if entry:
                return _pick(entry)
    return None


def _pick(rate: Rate) -> float | None:
    """AL Starting Max first, then the ceiling, then the middle."""
    return (
        rate.al_starting_max_cpm
        or rate.max_cpm
        or rate.average_cpm
        or rate.starting_cpm
    )


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
    # AL Starting Max first: it is what the buying team budgets from, and
    # the margin target of 50% is set against it. Max is the ceiling a
    # campaign may reach, not the rate it is planned at, and pacing on the
    # ceiling reads a campaign as cheaper than it was bought.
    return _pick(rate)
