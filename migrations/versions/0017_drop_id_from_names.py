"""Take the line item id back out of the names.

Two line items of the same product are the normal case, and the id was
appended to the name to tell them apart. The table shows the id in its own
column now, so the suffix is noise repeated twice on the same row.

Only a suffix that is this row's own id is removed, so a name a buyer typed
is left exactly as it is.

Revision ID: 0017_drop_id_from_names
Revises: 0016_line_item_status
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0017_drop_id_from_names"
down_revision = "0016_line_item_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        text(
            "SELECT id, name, external_id FROM line_items "
            "WHERE external_id IS NOT NULL AND name LIKE '% · %'"
        )
    ).all()
    for row_id, name, external_id in rows:
        suffix = f" · {external_id}"
        if name.endswith(suffix):
            conn.execute(
                text("UPDATE line_items SET name = :name WHERE id = :id"),
                {"name": name[: -len(suffix)], "id": row_id},
            )


def downgrade() -> None:
    """Nothing to put back - the id is on the row, in its own column."""
