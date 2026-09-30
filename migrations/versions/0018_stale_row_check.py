"""Count rows a newer report no longer mentions.

The delivery merge replaces per grain - (date, source, campaign, strategy) -
not per day. A strategy that stops mid-flight and drops out of later reports,
rather than reporting zeroes, leaves its old rows in place and the day reads
high by whatever it last served.

Counted per file, never deleted. Deleting on the strength of a report that
may be partial is how real delivery gets wiped; the count is what says
whether the case happens at all.

Revision ID: 0018_stale_row_check
Revises: 0017_drop_id_from_names
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_stale_row_check"
down_revision = "0017_drop_id_from_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingested_files", sa.Column("stale_rows", sa.Integer(), nullable=True))
    op.add_column(
        "ingested_files", sa.Column("stale_impressions", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("ingested_files", "stale_impressions")
    op.drop_column("ingested_files", "stale_rows")
