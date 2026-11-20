"""Pins kept on the server, seeded with kiri's favorites.

Revision ID: 0029_pinned_orders
Revises: 0028_page_snapshots
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_pinned_orders"
down_revision = "0028_page_snapshots"
branch_labels = None
depends_on = None

FAVORITES = ['139424', '137144', '137145', '124569', '137503', '132110', '135309', '96497', '123305', '137971', '127274', '138749', '133820', '100668', '133297', '135847', '136566', '139578', '132214', '135308']


def upgrade() -> None:
    op.create_table(
        "pinned_orders",
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), primary_key=True),
        sa.Column("pinned_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT DISTINCT order_id FROM line_items WHERE external_id IN :ids")
        .bindparams(sa.bindparam("ids", expanding=True)),
        {"ids": FAVORITES},
    ).scalars().all()
    for order_id in rows:
        bind.execute(sa.text("INSERT INTO pinned_orders (order_id) VALUES (:o)"), {"o": order_id})


def downgrade() -> None:
    op.drop_table("pinned_orders")
