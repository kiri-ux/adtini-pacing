"""Index delivery by line item.

Every page finds its delivery by `external_line_item_id`, and nothing indexed
it: each pacing table, order and product page read the whole of
`daily_delivery` to pick out its rows. Small while the table held a month,
seconds once it held years, on a database with 256MB to cache it in.

`ix_delivery_order_date` is in the models but no migration ever made it, so
a database built by migrations never had it. Both are built concurrently so
the ingest and the site keep running while they build.

Revision ID: 0020_delivery_line_item_index
Revises: 0019_weekly_checks
"""
from __future__ import annotations

from alembic import op

revision = "0020_delivery_line_item_index"
down_revision = "0019_weekly_checks"
branch_labels = None
depends_on = None

INDEXES = {
    "ix_delivery_line_item_date": "(external_line_item_id, date)",
    "ix_delivery_order_date": "(external_order_id, date)",
}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        for name, columns in INDEXES.items():
            op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON daily_delivery {columns}")
        return
    with op.get_context().autocommit_block():
        for name, columns in INDEXES.items():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON daily_delivery {columns}"
            )
        op.execute("ANALYZE daily_delivery")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_delivery_line_item_date")
