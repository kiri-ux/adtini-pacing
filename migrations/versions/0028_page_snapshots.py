"""Last good copy of each page, kept across deploys.

Revision ID: 0028_page_snapshots
Revises: 0027_months_running
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_page_snapshots"
down_revision = "0027_months_running"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "page_snapshots",
        sa.Column("path", sa.String(1000), primary_key=True),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("built_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_page_snapshots_built_at", "page_snapshots", ["built_at"])


def downgrade() -> None:
    op.drop_index("ix_page_snapshots_built_at", table_name="page_snapshots")
    op.drop_table("page_snapshots")
