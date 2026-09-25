"""A note can be about one product rather than the whole order.

"Lowered budget" on an order with five products is half an answer.

Revision ID: 0014_note_line_item
Revises: 0013_day_notes
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_note_line_item"
down_revision = "0013_day_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "day_notes", sa.Column("line_item_id", sa.Integer(), nullable=True)
    )
    op.create_index("ix_day_notes_line_item_id", "day_notes", ["line_item_id"])
    # SQLite cannot add a foreign key to an existing table; the column is
    # nullable and indexed either way, and Postgres gets the constraint.
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_day_notes_line_item", "day_notes", "line_items",
            ["line_item_id"], ["id"], ondelete="CASCADE",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint("fk_day_notes_line_item", "day_notes", type_="foreignkey")
    op.drop_index("ix_day_notes_line_item_id", table_name="day_notes")
    op.drop_column("day_notes", "line_item_id")
