"""A line item carries its own status.

The orders export has one per line - "IO Live", "IO Complete", "Cancelled" -
and it was being written onto the order, where whichever line imported last
spoke for all of them. An order shows as live while every line on it but one
has finished.

Revision ID: 0016_line_item_status
Revises: 0015_notes_into_the_log
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_line_item_status"
down_revision = "0015_notes_into_the_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("line_items", sa.Column("status", sa.String(60), nullable=True))


def downgrade() -> None:
    op.drop_column("line_items", "status")
