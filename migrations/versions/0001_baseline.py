"""Adopt whatever schema is already there and bring it up to current.

This is an **adoption baseline**, not a normal migration. It exists because
the first deploy built its tables with `create_all`, which creates missing
tables but never alters existing ones - so the columns added afterwards were
never applied and every page raised `UndefinedColumn`.

So it cannot assume it is running against an empty database. It reconciles
whatever it finds against the models: creates missing tables, adds missing
columns, and drops the table that no longer exists. That makes it safe on an
empty database, on the half-built one already deployed, and on one that is
already current.

Every revision after this one is an ordinary Alembic migration.

Revision ID: 0001_baseline
Revises:
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from models import Base

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

# Replaced by the id join between the two exports; delivery now carries
# `external_line_item_id` and finds its line item directly.
RETIRED_TABLES = ("delivery_mappings",)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Creates only what is absent, so an already-built database is untouched.
    Base.metadata.create_all(bind, checkfirst=True)

    existing = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue  # just created, so it is current by construction
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            op.add_column(table.name, _addable(column))

    for table in RETIRED_TABLES:
        if table in existing:
            op.drop_table(table)


def _addable(column: sa.Column) -> sa.Column:
    """A copy of a model column that can be added to a populated table.

    A NOT NULL column cannot be added to a table with rows unless it carries a
    server default, so one is supplied from the model's own default and the
    column is then left nullable-as-declared for new rows.
    """
    server_default = column.server_default
    if server_default is None and not column.nullable and column.default is not None:
        value = column.default.arg
        if not callable(value):
            if isinstance(value, bool):
                server_default = sa.text("1" if value else "0")
                if op.get_bind().dialect.name != "sqlite":
                    server_default = sa.text("true" if value else "false")
            elif isinstance(value, (int, float)):
                server_default = sa.text(str(value))
            elif isinstance(value, str):
                server_default = sa.text(f"'{value}'")

    return sa.Column(
        column.name,
        column.type,
        nullable=column.nullable,
        server_default=server_default,
    )


def downgrade() -> None:
    """Not meaningful: this revision adopts an existing schema rather than
    creating one, so there is no single prior state to return to."""
    raise NotImplementedError("the adoption baseline cannot be downgraded")
