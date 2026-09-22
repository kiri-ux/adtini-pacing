#!/usr/bin/env python
"""Seed the per-strategy sold splits from the buying team's sheets.

A one-time import, not a feed. The sheets are hand-kept, so reading them on
a schedule would have them fighting the nightly orders import; what lands
here is the tool's afterwards and is protected like any other entered term.

Sections are matched to orders by their order number where the sheet carries
one, and by client name and flight dates where it does not. Anything that
matches nothing is reported rather than dropped, so the gap is visible.

    python scripts/import_strategy_seed.py [--dry-run]
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sheets  # noqa: E402
from db import session_scope  # noqa: E402
from models import Client, Order, StrategyTerms  # noqa: E402
from sqlalchemy import select  # noqa: E402

SEED = Path(__file__).resolve().parent.parent / "data" / "strategy_seed.csv"


def _date(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _float(text: str) -> float | None:
    try:
        return float(text) if text else None
    except ValueError:
        return None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("seed")
    dry_run = "--dry-run" in sys.argv

    if not SEED.exists():
        log.error("no seed file at %s", SEED)
        return 1

    # Group the flat rows back into their sections.
    sections: dict[tuple, list[dict]] = defaultdict(list)
    with SEED.open(newline="") as handle:
        for row in csv.DictReader(handle):
            key = (row["client_name"], row["title"], row["order_number"],
                   row["start_date"], row["end_date"])
            sections[key].append(row)

    log.info("%s sections, %s strategy rows",
             len(sections), sum(len(v) for v in sections.values()))

    matched = unmatched = written = 0
    misses: list[str] = []

    with session_scope() as session:
        by_number = {
            o.external_order_id: o
            for o in session.execute(
                select(Order).where(Order.external_order_id.isnot(None))
            ).scalars()
        }
        by_client: dict[str, list[Order]] = defaultdict(list)
        for order, client_name in session.execute(
            select(Order, Client.name).join(Client, Client.id == Order.client_id)
        ):
            by_client[client_name.strip().lower()].append(order)

        # Terms already written for an order, kept across sections: several
        # sections can resolve to one order, and a section can name the same
        # strategy twice for two products. Either way the split is one row
        # per strategy per order.
        seen: dict[int, dict[str, StrategyTerms]] = {}

        for (client_name, title, number, start, end), rows in sections.items():
            order = by_number.get(number) if number else None

            if order is None:
                # No order number in the sheet, so fall back to the client
                # and the flight it describes.
                candidates = by_client.get(client_name.strip().lower(), [])
                start_date, end_date = _date(start), _date(end)
                exact = [
                    o for o in candidates
                    if o.start_date == start_date and o.end_date == end_date
                ]
                order = exact[0] if len(exact) == 1 else (
                    candidates[0] if len(candidates) == 1 else None
                )

            if order is None:
                unmatched += 1
                misses.append(f"{client_name} | {title}")
                continue

            matched += 1
            if dry_run:
                continue

            existing = seen.setdefault(
                order.id, {t.label: t for t in order.strategy_terms}
            )
            for position, row in enumerate(rows):
                label = row["strategy"]
                term = existing.get(label)
                if term is None:
                    term = StrategyTerms(order_id=order.id, label=label)
                    session.add(term)
                    existing[label] = term
                    written += 1
                    term.sort_order = position
                # A repeat carries the same split for another product, so the
                # order's total for that strategy is the sum of them.
                elif _float(row["total"]) is not None:
                    term.monthly_target = (term.monthly_target or 0) + (
                        _float(row["monthly"]) or 0
                    )
                    term.total_target = (term.total_target or 0) + _float(row["total"])
                    continue

                term.match_key = sheets.match_key(label)
                term.monthly_target = _float(row["monthly"])
                term.total_target = _float(row["total"])
                term.rate = _float(row["rate"])
                term.source = row["source"]

    log.info(
        "%s sections matched, %s not; %s strategy rows %s",
        matched, unmatched, written, "would be written" if dry_run else "written",
    )
    for miss in misses[:25]:
        log.info("  no order for: %s", miss)
    if len(misses) > 25:
        log.info("  ... and %s more", len(misses) - 25)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
