"""Null out the NaNs already stored in the sold terms.

A blank in the orders export arrived as a pandas NaN rather than None, was
waved past every "is this missing" guard, and was stored. Postgres keeps a
float NaN faithfully, so the goals and pacing percentages computed from those
columns all rendered as "nan" on the live pages. (SQLite has no NaN and its
driver writes one as NULL, which is why this never showed up locally.)

The parse and the guard are both fixed, but a stored NaN would survive them:
the guard's whole job is to not replace a figure with a blank, and a NaN
looks like a figure. So the rows already damaged are cleared here, once, and
the next orders import fills them from the file.

Revision ID: 0006_clear_nan_terms
Revises: 0005_campaign_links
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_clear_nan_terms"
down_revision = "0005_campaign_links"
branch_labels = None
depends_on = None

COLUMNS = (
    "monthly_impressions",
    "total_impressions",
    "goal_cpm",
    "monthly_spend",
    "total_spend",
    "goal_cpc",
    "client_monthly_budget",
    "client_total_budget",
    "google_monthly_spend",
    "google_total_spend",
    "goal_cpe",
    "monthly_events",
    "total_events",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # No other backend we run on can hold a NaN in a float column.
        return
    for column in COLUMNS:
        op.execute(
            sa.text(
                # Postgres treats NaN as equal to itself, so `x <> x` finds
                # nothing; it has to be compared to the literal.
                f"UPDATE line_items SET {column} = NULL "
                f"WHERE {column} = 'NaN'::double precision"
            )
        )


def downgrade() -> None:
    # Nothing to put back: the values were never meaningful.
    pass
