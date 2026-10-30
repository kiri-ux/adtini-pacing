"""Strategy targets, and delivery indexed by client.

`strategy_terms.targets` holds what a strategy targets - categories,
keywords, places - typed in on the details page until the adtini forms
carry it.

`ix_delivery_client_date` is in the models but no migration ever made it.
The order page looks a client's campaigns up by name, so without it that
lookup read the whole of `daily_delivery`. Built concurrently.

Revision ID: 0021_strategy_targets
Revises: 0020_delivery_line_item_index
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_strategy_targets"
down_revision = "0020_delivery_line_item_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("strategy_terms", sa.Column("targets", sa.Text(), nullable=True))
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_delivery_client_date "
            "ON daily_delivery (client_name, date)"
        )
        return
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_delivery_client_date "
            "ON daily_delivery (client_name, date)"
        )
        op.execute("ANALYZE daily_delivery")


def downgrade() -> None:
    op.drop_column("strategy_terms", "targets")
