"""Per-strategy sold terms, seeded from the hand-kept sheets.

Revision ID: 0004_strategy_terms
Revises: 0003_staging
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_strategy_terms"
down_revision = "0003_staging"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_terms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("label", sa.String(300), nullable=False),
        sa.Column("match_key", sa.String(120), nullable=True),
        sa.Column("monthly_target", sa.Float(), nullable=True),
        sa.Column("total_target", sa.Float(), nullable=True),
        sa.Column("rate", sa.Float(), nullable=True),
        sa.Column("source", sa.String(200), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("order_id", "label", name="uq_strategy_terms_label"),
    )
    op.create_index("ix_strategy_terms_order_id", "strategy_terms", ["order_id"])
    op.create_index("ix_strategy_terms_match_key", "strategy_terms", ["match_key"])


def downgrade() -> None:
    op.drop_table("strategy_terms")
