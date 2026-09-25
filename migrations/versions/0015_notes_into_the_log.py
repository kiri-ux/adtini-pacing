"""Move the order's notes and adjustment into the day log.

The order carried three free-text fields - a monthly note, an adjustment and
the date it was adjusted on - from before there was anywhere better to put
them. The day log is that place now: dated, attributed, and as many as you
like rather than one that the next edit overwrites.

Carried across rather than dropped. Somebody typed them.

Revision ID: 0015_notes_into_the_log
Revises: 0014_note_line_item
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_notes_into_the_log"
down_revision = "0014_note_line_item"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, notes, adjustment_note, last_adjusted_on, start_date "
            "FROM orders "
            "WHERE (notes IS NOT NULL AND notes <> '') "
            "   OR (adjustment_note IS NOT NULL AND adjustment_note <> '')"
        )
    ).fetchall()

    for order_id, notes, adjustment, adjusted_on, start_date in rows:
        # A note has to sit on a day. The day it was adjusted when that is
        # known, otherwise the day the flight started.
        for body in (adjustment, notes):
            if not body or not str(body).strip():
                continue
            bind.execute(
                sa.text(
                    "INSERT INTO day_notes (order_id, date, body, author) "
                    "VALUES (:order_id, :date, :body, :author)"
                ),
                {
                    "order_id": order_id,
                    "date": adjusted_on or start_date or sa.func.current_date(),
                    "body": str(body).strip(),
                    "author": "from the order",
                },
            )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM day_notes WHERE author = 'from the order'"))
