"""Unpin the stand-in orders made for delivery with no order id.

0029 pinned every order holding one of the favorite line items, which
took in those stand-ins (no order id, no buyer) beside the real orders.

Revision ID: 0030_unpin_adopted_orders
Revises: 0029_pinned_orders
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_unpin_adopted_orders"
down_revision = "0029_pinned_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(sa.text(
        "DELETE FROM pinned_orders WHERE order_id IN "
        "(SELECT id FROM orders WHERE external_order_id IS NULL)"
    ))


def downgrade() -> None:
    pass
