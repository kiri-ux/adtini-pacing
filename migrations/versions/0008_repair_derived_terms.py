"""Repair the two sold terms that are derived rather than imported.

The setup CPM is a rate card lookup and the sold total is the monthly figure
over the months the line item runs. Neither comes from the orders export, and
both were wrong in the stored data - the rate card was not deployed at all
for a while, and several parser faults put a ratio artifact where the total
belongs. Total pacing read in the millions of percent.

Fixing the code fixes what is imported next, and a sweep skips a file it has
already read, so every orders export would have to be read again - gigabytes
of them - for two columns the files do not carry. There is a button for this
on the Data page, but it is a thing to remember and a thing to get wrong, so
it runs here instead: on deploy, before the service starts, in the one-off
container rather than in a web worker with pages to serve.

Repeating it is harmless. It only touches rows that are wrong, and never a
row a buyer has edited by hand.

Revision ID: 0008_repair_derived_terms
Revises: 0007_delivery_campaign_index
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_repair_derived_terms"
down_revision = "0007_delivery_campaign_index"
branch_labels = None
depends_on = None


def _months_expression(dialect: str, start: str, end: str) -> str:
    """Calendar months a flight touches, counting both ends, in SQL.

    1 August to 31 January is six months. Postgres and SQLite pull the year
    and month out of a date differently and neither borrows the other's
    spelling.
    """
    if dialect == "postgresql":
        y = "EXTRACT(YEAR FROM {})"
        m = "EXTRACT(MONTH FROM {})"
    else:
        y = "CAST(strftime('%Y', {}) AS INTEGER)"
        m = "CAST(strftime('%m', {}) AS INTEGER)"
    return (
        f"(({y.format(end)} - {y.format(start)}) * 12"
        f" + ({m.format(end)} - {m.format(start)}) + 1)"
    )


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # --- the setup CPM, from the rate card -------------------------------
    # `ratecard` only reads a CSV, so importing it here couples this to the
    # card's contents rather than to the schema - it cannot drift the way
    # importing the models would.
    import ratecard

    pairs = bind.execute(
        sa.text(
            "SELECT DISTINCT product, restricted FROM line_items "
            "WHERE product IS NOT NULL"
        )
    ).fetchall()

    for product, restricted in pairs:
        cpm = ratecard.setup_cpm(product, restricted=bool(restricted))
        if not cpm:
            continue
        bind.execute(
            sa.text(
                "UPDATE line_items SET goal_cpm = :cpm, goal_cpm_source = 'rate card' "
                "WHERE product = :product AND restricted = :restricted "
                "AND goal_cpm IS NULL AND terms_locked = :false_"
            ),
            {
                "cpm": cpm,
                "product": product,
                "restricted": restricted,
                "false_": False,
            },
        )

    # --- the sold total, from the monthly figure over the flight ---------
    start = "COALESCE(li.start_date, o.start_date)"
    end = "COALESCE(li.end_date, o.end_date)"
    months = _months_expression(dialect, start, end)

    # A total smaller than one month of itself is the wrong number whatever
    # supplied it. Rebuilt where the flight says how long it runs.
    condition = (
        "li.monthly_impressions IS NOT NULL AND li.monthly_impressions > 0 "
        "AND li.total_impressions IS NOT NULL "
        "AND li.total_impressions < li.monthly_impressions "
        "AND li.terms_locked = :false_"
    )

    if dialect == "postgresql":
        bind.execute(
            sa.text(
                f"UPDATE line_items li SET total_impressions = "
                f"li.monthly_impressions * {months} "
                f"FROM orders o WHERE o.id = li.order_id AND {condition} "
                f"AND {start} IS NOT NULL AND {end} IS NOT NULL AND {months} > 0"
            ),
            {"false_": False},
        )
        # Nothing to rebuild it from: a dash reads as unset, where a wrong
        # number reads as a fact.
        bind.execute(
            sa.text(
                f"UPDATE line_items li SET total_impressions = NULL "
                f"FROM orders o WHERE o.id = li.order_id AND {condition}"
            ),
            {"false_": False},
        )
    else:
        joined = (
            "SELECT o.id FROM orders o WHERE o.id = line_items.order_id"
        )
        sub_start = "COALESCE(line_items.start_date, (SELECT o.start_date FROM orders o WHERE o.id = line_items.order_id))"
        sub_end = "COALESCE(line_items.end_date, (SELECT o.end_date FROM orders o WHERE o.id = line_items.order_id))"
        sub_months = _months_expression(dialect, sub_start, sub_end)
        sub_condition = condition.replace("li.", "line_items.")
        bind.execute(
            sa.text(
                f"UPDATE line_items SET total_impressions = "
                f"line_items.monthly_impressions * {sub_months} "
                f"WHERE {sub_condition} AND {sub_start} IS NOT NULL "
                f"AND {sub_end} IS NOT NULL AND {sub_months} > 0"
            ),
            {"false_": False},
        )
        bind.execute(
            sa.text(
                f"UPDATE line_items SET total_impressions = NULL "
                f"WHERE {sub_condition}"
            ),
            {"false_": False},
        )


def downgrade() -> None:
    # The values this replaced were never meaningful.
    pass
