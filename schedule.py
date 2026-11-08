"""The buyers' update days, from the Weekly Campaign Update Schedule sheet.

The sheet (tab "Update Schedule") is the source of truth for who updates
which partner on which day. Its layout:

- A-D: every partner with its buyer and the buyer's email.
- H-J, L-N, P-R, T-V, X-Z: Monday to Friday, each "<Day> Markets", "#",
  "Buyer". Each day's list runs down from row 2 until a blank or a
  "Total ..." cell; below that the sheet keeps per-buyer counts, which are
  not partners.

The file it reads is the tab saved as CSV (File > Download > CSV). A copy
ships in data/update_schedule.csv so a fresh database has a schedule.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func, select

from models import Client, Order, ScheduleEntry

SNAPSHOT = Path(__file__).parent / "data" / "update_schedule.csv"

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
# Column index of each day's partner name; "#" and "Buyer" follow it.
DAY_COLUMNS = (7, 11, 15, 19, 23)


@dataclass(frozen=True)
class Entry:
    market: str
    weekday: int
    buyer: str
    email: str | None = None


def _cell(row: list[str], i: int) -> str:
    return row[i].strip() if i < len(row) else ""


def parse(text: str) -> list[Entry]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    for day, col in zip(WEEKDAYS, DAY_COLUMNS):
        if col >= len(header) or not header[col].startswith(day.lower()):
            raise ValueError(f"Column {col + 1} should be {day} Markets.")

    emails: dict[str, str] = {}
    for row in rows[1:]:
        buyer, email = _cell(row, 2), _cell(row, 3)
        if buyer and "@" in email:
            emails.setdefault(buyer, email)

    out: list[Entry] = []
    for weekday, col in enumerate(DAY_COLUMNS):
        for row in rows[1:]:
            market = _cell(row, col)
            if not market or market.lower().startswith("total"):
                break
            buyer = _cell(row, col + 2)
            if not buyer:
                continue
            out.append(Entry(market, weekday, buyer, emails.get(buyer)))
    return out


def load(session, text: str) -> int:
    """Replace the whole schedule with the sheet's. Returns rows loaded."""
    entries = parse(text)
    if not entries:
        raise ValueError("No partners found in the file.")
    session.execute(delete(ScheduleEntry))
    session.add_all(
        ScheduleEntry(market=e.market, weekday=e.weekday, buyer=e.buyer, email=e.email)
        for e in entries
    )
    # The sessions run without autoflush; the page that seeded the schedule
    # reads it back in the same session.
    session.flush()
    return len(entries)


def seed_if_empty(session) -> None:
    if session.execute(select(func.count()).select_from(ScheduleEntry)).scalar():
        return
    if SNAPSHOT.exists():
        load(session, SNAPSHOT.read_text(encoding="utf-8"))


def key(name: str | None) -> str:
    """Partner names compared without case, punctuation or spacing.

    The sheet and the orders drop agree on partner names but not always on
    "Fresno, CA" vs "Fresno CA" or a doubled space.
    """
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def buyers(session) -> list[str]:
    return sorted(
        session.execute(select(ScheduleEntry.buyer).distinct()).scalars()
    )


def all_markets(session) -> list[tuple[str, int, str]]:
    """(partner, weekday, buyer) for every scheduled partner, A to Z."""
    rows = session.execute(
        select(ScheduleEntry.market, ScheduleEntry.weekday, ScheduleEntry.buyer)
    ).all()
    return sorted(((m, w, b) for m, w, b in rows), key=lambda r: (r[0].lower(), r[1]))


def day_markets(session, buyer: str, weekday: int) -> list[str]:
    return list(
        session.execute(
            select(ScheduleEntry.market)
            .where(ScheduleEntry.buyer == buyer, ScheduleEntry.weekday == weekday)
            .order_by(ScheduleEntry.market)
        ).scalars()
    )


def matching_markets(session, names: list[str]) -> tuple[list[str], list[str]]:
    """The Client.market values for schedule partner names.

    Returns (market values to filter on, schedule names with no orders).
    """
    wanted = {key(n): n for n in names}
    values = session.execute(
        select(Client.market).where(Client.market.isnot(None)).distinct()
    ).scalars()
    found: list[str] = []
    seen: set[str] = set()
    for value in values:
        k = key(value)
        if k in wanted:
            found.append(value)
            seen.add(k)
    missing = [n for k, n in wanted.items() if k not in seen]
    return found, missing


def today_weekday(today: dt.date | None = None) -> int:
    """Today as a schedule day; Saturday and Sunday read as Monday."""
    wd = (today or dt.date.today()).weekday()
    return wd if wd < 5 else 0


def by_day(session, weekday: int) -> dict[str, int]:
    """Buyer -> number of partners on that day."""
    rows = session.execute(
        select(ScheduleEntry.buyer, func.count())
        .where(ScheduleEntry.weekday == weekday)
        .group_by(ScheduleEntry.buyer)
    ).all()
    return {b: n for b, n in sorted(rows)}


def order_buyer(session, buyer: str) -> str | None:
    """The orders drop's buyer name for a schedule buyer.

    The sheet uses first names or nicknames ("Laura", "Bree", "Renn"); the
    orders drop uses full names ("Laura Ostrowski", "Brianne Baccarini",
    "Dan Renninger"). Matched on the sheet's email first (the local part
    holds the last name, or starts it: renn@ -> Renninger), then on a
    unique first name.
    """
    names = [
        n for n in session.execute(
            select(Order.buyer).where(Order.buyer.isnot(None)).distinct()
        ).scalars() if n.strip()
    ]
    email = session.execute(
        select(ScheduleEntry.email)
        .where(ScheduleEntry.buyer == buyer, ScheduleEntry.email.isnot(None))
        .limit(1)
    ).scalar()
    local = key((email or "").split("@")[0])
    if local:
        hits = []
        for name in names:
            parts = [key(p) for p in name.split()]
            last = parts[-1] if len(parts) > 1 else ""
            if last and (last in local or (len(local) >= 4 and last.startswith(local))):
                hits.append(name)
        if len(hits) == 1:
            return hits[0]
    # "Matt H" is not Matt Ogden: only a bare first name falls back to it.
    if len(buyer.split()) != 1:
        return None
    first = key(buyer)
    hits = [n for n in names if key(n.split()[0]) == first]
    return hits[0] if len(hits) == 1 else None


def day_market_values(session, weekday: int) -> list[str]:
    """Client.market values for every partner on the schedule that day."""
    names = list(
        session.execute(
            select(ScheduleEntry.market).where(ScheduleEntry.weekday == weekday)
        ).scalars()
    )
    return matching_markets(session, names)[0]
