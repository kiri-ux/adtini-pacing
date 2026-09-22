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
