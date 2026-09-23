"""A strategy label is unique within a product, not within an order.

An order can carry two line items of the same product - two Mobile
Conquesting lines, say - and both running "MC - Behavioral" is two real rows
describing two real campaigns, not a duplicate. Keyed on the order alone, the
second one could not be written at all.

Revision ID: 0011_strategy_label_per_product
Revises: 0010_strategy_per_line_item
"""
from __future__ import annotations

from alembic import op

revision = "0011_strategy_label_per_product"
down_revision = "0010_strategy_per_line_item"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("strategy_terms") as batch:
        batch.drop_constraint("uq_strategy_terms_label", type_="unique")
        batch.create_unique_constraint(
            "uq_strategy_terms_label", ["order_id", "line_item_id", "label"]
        )


def downgrade() -> None:
    with op.batch_alter_table("strategy_terms") as batch:
        batch.drop_constraint("uq_strategy_terms_label", type_="unique")
        batch.create_unique_constraint(
            "uq_strategy_terms_label", ["order_id", "label"]
        )
