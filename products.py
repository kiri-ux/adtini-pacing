"""The product registry: abbreviation, chip colour, and whether it paces.

From the buying team's own product sheet, versioned here so a change to it
is a reviewable commit rather than an edit somewhere else that silently
shifts what the pages show.

Not everything sold is media that paces. Website Visitor ID, Live Chat, SEO,
reputation management, analytics integrations and the management-fee lines
are on orders but have no impressions or spend to pace, so they are carried
here as `paced = 0` and left out of the pacing views entirely.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CSV_PATH = Path(__file__).parent / "data" / "products.csv"


@dataclass(frozen=True)
class Product:
    name: str
    abbreviation: str
    hex: str
    paced: bool

    @property
    def text_hex(self) -> str:
        """Ink that stays legible on this chip.

        The palette runs from near-black to near-white, so a fixed text
        colour would be unreadable on half of it.
        """
        return "#FFFFFF" if _luminance(self.hex) < 0.5 else "#1A2330"


def _luminance(hex_colour: str) -> float:
    """Perceived lightness, 0 (black) to 1 (white)."""
    value = hex_colour.lstrip("#")
    if len(value) != 6:
        return 1.0
    r, g, b = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _key(name: str) -> str:
    """Product names differ in punctuation and case between exports."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


@lru_cache(maxsize=1)
def registry() -> dict[str, Product]:
    out: dict[str, Product] = {}
    if not CSV_PATH.exists():
        return out
    with CSV_PATH.open(newline="") as handle:
        for row in csv.DictReader(handle):
            product = Product(
                name=row["product"],
                abbreviation=row["abbreviation"],
                hex=row["hex"],
                paced=row["paced"] == "1",
            )
            out[_key(product.name)] = product
    return out


# The delivery feed's shorter names, mapped onto the sheet's own.
FEED_ALIASES = {
    "meta": "Meta Display & Video Ads",
    "display": "Display Ads",
    "native display": "Native Display Ads",
    "social mirror": "Social Mirror Ads",
    "social mirror ctv": "Social Mirror CTV Ads",
    "ctv": "Connected TV Ads",
    "video": "Video Ads",
    "native video": "Native Video Ads",
    "online audio": "Online Audio Ads",
    "mobile": "Mobile Conquesting Display & Video Ads",
    "tiktok": "TikTok Display & Video Ads",
    "linkedin": "Linkedin Ads",
    "ppc": "Pay-Per-Click Ads",
    "pmax": "Performance Max Ads",
    "youtube+": "YouTube Video Ads",
    "youtube tv": "YouTube Video Ads",
    "amazon premium display": "Amazon Premium Display Ads",
    "amazon premium video": "Amazon Premium Video (with Twitch) & OTT Ads",
    "amazon premium ctv": "Amazon Premium Video (with Twitch) & OTT Ads",
    "digital out-of-home": "Digital Out-Of-Home (DOOH) Display & Video Ads",
    "geo-framing": "Geo-Framing Display Ads",
    "seo": "Search Engine Optimization+",
}


def lookup(name: str | None) -> Product | None:
    if not name:
        return None
    entries = registry()
    found = entries.get(_key(name))
    if found:
        return found
    alias = FEED_ALIASES.get((name or "").strip().lower())
    return entries.get(_key(alias)) if alias else None


def abbreviation(name: str | None) -> str:
    product = lookup(name)
    if product:
        return product.abbreviation
    return (name or "")[:4].upper()


def is_paced(name: str | None) -> bool:
    """Whether this product has anything to pace.

    An unrecognised product is paced: a new media product should show up and
    be noticed, not be silently dropped for not being in the sheet yet.
    """
    product = lookup(name)
    return product.paced if product else True


# The prefix a strategy label carries, mapped onto the product it names.
# The sheets were kept by hand over years and never agreed on a spelling:
# Meta is FB, FB/IG, FB/Insta and Meta; Performance Max is PMAX and PM.
STRATEGY_PREFIXES = {
    "sm": "Social Mirror Ads",
    "social mirror": "Social Mirror Ads",
    "smc": "Social Mirror CTV Ads",
    "sm ctv": "Social Mirror CTV Ads",
    "sm ott": "Social Mirror CTV Ads",
    "mc": "Mobile Conquesting Display & Video Ads",
    "mobile": "Mobile Conquesting Display & Video Ads",
    "mobile conquesting": "Mobile Conquesting Display & Video Ads",
    "fb": "Meta Display & Video Ads",
    "fb/ig": "Meta Display & Video Ads",
    "fb/insta": "Meta Display & Video Ads",
    "meta": "Meta Display & Video Ads",
    "ctv": "Connected TV Ads",
    "ott": "Connected TV Ads",
    "d": "Display Ads",
    "dis": "Display Ads",
    "display": "Display Ads",
    "v": "Video Ads",
    "video": "Video Ads",
    "nv": "Native Video Ads",
    "nd": "Native Display Ads",
    "geo": "Geo-Framing Display Ads",
    "gf": "Geo-Framing Display Ads",
    "geo-framing": "Geo-Framing Display Ads",
    "audio": "Online Audio Ads",
    "oa": "Online Audio Ads",
    "pmax": "Performance Max Ads",
    "pm": "Performance Max Ads",
    "performance max": "Performance Max Ads",
    "ppc": "Pay-Per-Click Ads",
    "az": "Amazon Premium Display Ads",
    "ad": "Amazon Premium Display Ads",
    "av": "Amazon Premium Video (with Twitch) & OTT Ads",
    "yt": "YouTube Video Ads",
    "youtube": "YouTube Video Ads",
    "tt": "TikTok Display & Video Ads",
    "tiktok": "TikTok Display & Video Ads",
    "li": "Linkedin Ads",
    "linkedin": "Linkedin Ads",
    "cv": "CTV + Video Ads",
    "dooh": "Digital Out-Of-Home (DOOH) Display & Video Ads",
}


def product_for_strategy(label: str | None) -> str | None:
    """The product a strategy label names, from the part before the targeting.

    "FB - Retargeting" is Meta's retargeting, not the order's. Longest
    prefix first, so "SM CTV" is not read as "SM".
    """
    text = (label or "").strip().lower()
    if not text:
        return None
    for prefix in sorted(STRATEGY_PREFIXES, key=len, reverse=True):
        if text == prefix or text.startswith(prefix + " ") or text.startswith(prefix + "-"):
            return STRATEGY_PREFIXES[prefix]
    return None
