"""A campaign matched on its line item id can be left out of that line item.

Revision ID: 0026_excluded_campaign_links
Revises: 0025_shared_campaign_links
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_excluded_campaign_links"
down_revision = "0025_shared_campaign_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("campaign_links") as batch:
        batch.add_column(
            sa.Column("excluded", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("campaign_links") as batch:
        batch.drop_column("excluded")
