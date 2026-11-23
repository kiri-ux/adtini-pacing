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


@dataclass(frozen=True)
class Person:
    name: str

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.replace("-", " ").split() if p[:1].isalpha()]
        return "".join(p[0].upper() for p in parts[:2]) or self.name[:2].upper()


@dataclass(frozen=True)
class Team:
    buyer: Person | None
    paid_media: Person | None


def _key(text: str | None) -> str:
    return " ".join((text or "").lower().split())


@lru_cache(maxsize=1)
def _sheet() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
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
             known: list[str] | tuple[str, ...] = ()) -> Team:
    """The partner's buyer and paid media buyer. The order's own buyer
    stands in for a partner the sheet does not list."""
    buyer, pmb = _sheet().get(_key(partner), (None, None))
    buyer_name = full_name(buyer, known) if buyer else order_buyer
    pmb_name = full_name(pmb, known) if pmb else None
    return Team(
        buyer=Person(buyer_name) if buyer_name else None,
        paid_media=Person(pmb_name) if pmb_name else None,
    )
