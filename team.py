"""Who buys for each partner: the main buyer and the paid media buyer.

From the Reporting Breakout sheet's Market Breakout tab
(1_WfyDOEN4oOdPdEoYtv5yMZZ6_hk8vu5o6pElIJ-8d4), mirrored in
data/partner_buyers.csv. The sheet gives first names; full names come
from NAMES, then from the buyer names on the orders.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SNAPSHOT = Path(__file__).parent / "data" / "partner_buyers.csv"

# The sheet's name -> full name.
NAMES = {
    "Alyssa": "Alyssa Frank",
    "Amin": "Amin Vora",
    "Bella": "Bella Duddy",
    "Carly": "Carly Manna",
    "Hanna": "Hanna Walentukonis",
    "Jacob": "Jacob Rauhut",
    "John": "John Ferruzzi",
    "Katie Oxman": "Katie Oxman",
    "Laura": "Laura Ostrowski",
    "Lauren": "Lauren Hunter",
    "Luke": "Luke Catolico",
    "Mallory": "Mallory Hoenig",
    "Matt": "Matt Ogden",
    "Matt H": "Matthew Hinckley",
    "Nicolette": "Nicolette Alessi",
    "Taylor": "Taylor Rhodes",
    "Hana": "Hana Mura",
    "Stacy": "Stacy Sroka",
    "Olivia": "Olivia Gregus",
}


# Initials kiri set where the sheet has no last name (11/23).
INITIALS = {"Renn": "DR", "Bree": "BB"}

# Outline colors, from the product chip palette in the adtini hex codes
# sheet (1VB4IpDRPYbhkz2T4ke_MIvaS8UWnEE3ljxs-73tzUpI). One per person.
COLORS = {
    "Hana": "#DC143C", "Stacy": "#483D8B", "Olivia": "#2E8B57", "Bree": "#F4A300",
    "Alyssa": "#9966CC", "Amin": "#20B2AA", "Bella": "#FF4500", "Carly": "#008080",
    "Hanna": "#F88379", "Jacob": "#4B4B4B", "John": "#87CEEB", "Katie Oxman": "#FFD700",
    "Laura": "#40E0D0", "Lauren": "#2A3439", "Luke": "#8B8B8B", "Mallory": "#7FFFD4",
    "Matt": "#E0B0FF", "Matt H": "#D8BFD8", "Nicolette": "#B2BEB5", "Renn": "#FFDAB9",
    "Taylor": "#98FB98",
}


# Products the paid media buyer runs (social, search, YouTube). Every
# other ad product is the buyer's. Services that are not ad buys need
# neither.
PAID_MEDIA_CODES = {"M", "MM", "ML", "TT", "LI", "PPC", "PM", "PMM", "YT"}
SERVICE_CODES = {"SEO", "ORM", "LC", "GA4", "ID"}


@dataclass(frozen=True)
class Person:
    name: str
    short: str | None = None  # the sheet's name for them

    @property
    def initials(self) -> str:
        if self.short in INITIALS:
            return INITIALS[self.short]
        parts = [p for p in self.name.replace("-", " ").split() if p[:1].isalpha()]
        return "".join(p[0].upper() for p in parts[:2]) or self.name[:2].upper()

    @property
    def color(self) -> str | None:
        return COLORS.get(self.short or "")


@dataclass(frozen=True)
class Team:
    buyer: Person | None
    paid_media: Person | None


def _key(text: str | None) -> str:
    return " ".join((text or "").lower().split())


@lru_cache(maxsize=1)
def _sheet() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    if not SNAPSHOT.exists():
        return out
    with SNAPSHOT.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[_key(row["partner"])] = (row["buyer"].strip(), row["paid_media_buyer"].strip())
    return out


def full_name(name: str | None, known: list[str] | tuple[str, ...] = ()) -> str | None:
    """The sheet's short name as a full name, else one of `known` (order
    buyer names) that starts with it, else the name as given."""
    name = (name or "").strip()
    if not name:
        return None
    if name in NAMES:
        return NAMES[name]
    first, _, rest = name.partition(" ")
    for full in known:
        parts = (full or "").split()
        if parts and parts[0].lower() == first.lower() and (
            not rest or (len(parts) > 1 and parts[1].lower().startswith(rest.lower()))
        ):
            return full
    return name


def team_for(partner: str | None, order_buyer: str | None = None,
             known: list[str] | tuple[str, ...] = (),
             codes: list[str] | tuple[str, ...] | set[str] | None = None) -> Team:
    """The partner's buyer and paid media buyer. The order's own buyer
    stands in for a partner the sheet does not list.

    With `codes` (the order's product codes), only the people who run
    those products: the buyer for programmatic products, the paid media
    buyer for PAID_MEDIA_CODES."""
    buyer, pmb = _sheet().get(_key(partner), (None, None))
    if codes:
        codes = {c.upper() for c in codes if c}
        if not codes & PAID_MEDIA_CODES:
            pmb = None
        if not codes - PAID_MEDIA_CODES - SERVICE_CODES:
            buyer, order_buyer = None, None
    buyer_name = full_name(buyer, known) if buyer else order_buyer
    pmb_name = full_name(pmb, known) if pmb else None
    return Team(
        buyer=Person(buyer_name, buyer) if buyer_name else None,
        paid_media=Person(pmb_name, pmb) if pmb_name else None,
    )


def runs(team: Team | None, name: str | None, full: str | None = None) -> bool:
    """Whether `name` (a first name or the sheet's name) or `full` is the
    order's buyer or paid media buyer."""
    if team is None or not (name or full):
        return False
    first = (name or "").split()[0].lower() if name else ""
    for person in (team.buyer, team.paid_media):
        if person is None:
            continue
        if full and person.name == full:
            return True
        if name and (person.short == name or person.name.lower() == name.lower()
                     or person.name.split()[0].lower() == first):
            return True
    return False
