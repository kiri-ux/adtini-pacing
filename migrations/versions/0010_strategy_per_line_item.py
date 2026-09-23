"""Hang a strategy off the product it runs under, and let one be added.

A strategy is targeting within a product - "FB - Retargeting" is Meta's
retargeting, not the order's - so pacing it needs that product's dates and
rate, which means knowing which product it belongs to.

`added_by_hand` marks a row a buyer typed rather than one seeded from a
sheet, because extra targeting gets bought mid-flight and the order data will
never mention it.

Revision ID: 0010_strategy_per_line_item
Revises: 0009_line_item_pacing_type
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_strategy_per_line_item"
down_revision = "0009_line_item_pacing_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategy_terms", sa.Column("line_item_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "strategy_terms",
        sa.Column(
            "added_by_hand", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_index(
        "ix_strategy_terms_line_item_id", "strategy_terms", ["line_item_id"]
    )
    # SQLite cannot add a foreign key to an existing table, and the column is
    # nullable with an index either way; Postgres gets the constraint.
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_strategy_terms_line_item",
            "strategy_terms",
            "line_items",
            ["line_item_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(
            "fk_strategy_terms_line_item", "strategy_terms", type_="foreignkey"
        )
    op.drop_index("ix_strategy_terms_line_item_id", table_name="strategy_terms")
    op.drop_column("strategy_terms", "added_by_hand")
    op.drop_column("strategy_terms", "line_item_id")
