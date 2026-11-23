"""Page build timings for /timings.

Revision ID: 0032_render_timings
Revises: 0031_cache_edits
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_render_timings"
down_revision = "0031_cache_edits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "render_timings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("path", sa.String(1000), nullable=False),
        sa.Column("seconds", sa.Float(), nullable=False),
        sa.Column("steps", sa.String(2000), nullable=True),
        sa.Column("background", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_render_timings_at", "render_timings", ["at"])


def downgrade() -> None:
    op.drop_index("ix_render_timings_at", table_name="render_timings")
    op.drop_table("render_timings")
