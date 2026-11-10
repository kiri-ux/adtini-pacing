"""How many months a line item is sold for.

Revision ID: 0027_months_running
Revises: 0026_excluded_campaign_links
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_months_running"
down_revision = "0026_excluded_campaign_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("line_items") as batch:
        batch.add_column(sa.Column("months_running", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("line_items") as batch:
        batch.drop_column("months_running")
