"""Adopt whatever schema is already there and bring it to the v2 baseline.

This is an **adoption baseline**, not a normal migration. It exists because
the first deploy built its tables with `create_all`, which creates missing
tables but never alters existing ones - so the columns added afterwards were
never applied and every page raised `UndefinedColumn`.

So it cannot assume it is running against an empty database. It reconciles
whatever it finds: creates missing tables, adds missing columns, and drops
the table that no longer exists. That makes it safe on an empty database, on
the half-built one already deployed, and on one already current.

The schema below is **frozen at the commit this baseline was written**, and
deliberately not read from `models`. A baseline that builds from live models
recreates whatever the models currently say, so every later migration then
tries to add a column the baseline already made - which is exactly what
happened on the first attempt at this file.

Every revision after this one is an ordinary Alembic migration.

Revision ID: 0001_baseline
Revises:
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

METADATA = sa.MetaData()

# Replaced by the id join between the two exports; delivery now carries
# `external_line_item_id` and finds its line item directly.
RETIRED_TABLES = ("delivery_mappings",)

sa.Table(
    "clients",
    METADATA,
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('name', sa.String(length=300), nullable=False),
    sa.Column('market', sa.String(length=200), nullable=True),
    sa.Column('buyer', sa.String(length=120), nullable=True),
    sa.Column('container_tag', sa.Boolean(), nullable=True),
)
sa.Table(
    "daily_delivery",
    METADATA,
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('data_source', sa.String(length=120), nullable=False),
    sa.Column('campaign_id', sa.String(length=64), nullable=False),
    sa.Column('strategy_id', sa.String(length=64), nullable=False),
    sa.Column('business_unit', sa.String(length=200), nullable=True),
    sa.Column('client_name', sa.String(length=300), nullable=True),
    sa.Column('external_order_id', sa.String(length=64), nullable=True),
    sa.Column('external_line_item_id', sa.String(length=64), nullable=True),
    sa.Column('order_level_name', sa.String(length=400), nullable=True),
    sa.Column('line_item_name', sa.String(length=400), nullable=True),
    sa.Column('strategy_name', sa.String(length=400), nullable=True),
    sa.Column('strategy_type', sa.String(length=120), nullable=True),
    sa.Column('product', sa.String(length=120), nullable=True),
    sa.Column('campaign_name', sa.String(length=400), nullable=True),
    sa.Column('campaign_start_date', sa.Date(), nullable=True),
    sa.Column('impressions', sa.Float(), nullable=False),
    sa.Column('clicks', sa.Float(), nullable=False),
    sa.Column('cost', sa.Float(), nullable=False),
    sa.Column('conversions', sa.Float(), nullable=False),
    sa.Column('viewthroughs', sa.Float(), nullable=False),
    sa.Column('click_conversions', sa.Float(), nullable=False),
    sa.Column('goal_cpm', sa.Float(), nullable=True),
    # server_default is load-bearing: the ingest inserts in bulk without
    # setting it, so a column without one fails NOT NULL on every row.
    sa.Column('updated_at', sa.DateTime(), nullable=False,
              server_default=sa.func.now()),
    sa.UniqueConstraint('date', 'data_source', 'campaign_id', 'strategy_id', name='uq_delivery_grain'),
)
sa.Table(
    "ingested_files",
    METADATA,
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('s3_key', sa.String(length=600), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('etag', sa.String(length=120), nullable=True),
    sa.Column('size_bytes', sa.Integer(), nullable=True),
    sa.Column('rows_read', sa.Integer(), nullable=True),
    sa.Column('rows_written', sa.Integer(), nullable=True),
    sa.Column('min_date', sa.Date(), nullable=True),
    sa.Column('max_date', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('unmapped_columns', sa.Text(), nullable=True),
    sa.Column('ingested_at', sa.DateTime(), nullable=False,
              server_default=sa.func.now()),
)
sa.Table(
    "orders",
    METADATA,
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('client_id', sa.Integer(), sa.ForeignKey('clients.id'), nullable=False),
    sa.Column('external_order_id', sa.String(length=64), nullable=True),
    sa.Column('name', sa.String(length=400), nullable=False),
    sa.Column('pacing_type', sa.String(length=20), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=True),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('buyer', sa.String(length=120), nullable=True),
    sa.Column('order_type', sa.String(length=60), nullable=True),
    sa.Column('status', sa.String(length=60), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('paused', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('last_adjusted_on', sa.Date(), nullable=True),
    sa.Column('adjustment_note', sa.String(length=300), nullable=True),
    sa.Column('terms_locked', sa.Boolean(), nullable=False),
    sa.UniqueConstraint('client_id', 'name', name='uq_order_client_name'),
)
sa.Table(
    "line_items",
    METADATA,
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('order_id', sa.Integer(), sa.ForeignKey('orders.id'), nullable=False),
    sa.Column('name', sa.String(length=400), nullable=False),
    sa.Column('product', sa.String(length=120), nullable=True),
    sa.Column('strategy_type', sa.String(length=120), nullable=True),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('external_id', sa.String(length=64), nullable=True),
    sa.Column('terms_locked', sa.Boolean(), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=True),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('monthly_impressions', sa.Float(), nullable=True),
    sa.Column('total_impressions', sa.Float(), nullable=True),
    sa.Column('goal_cpm', sa.Float(), nullable=True),
    sa.Column('monthly_spend', sa.Float(), nullable=True),
    sa.Column('total_spend', sa.Float(), nullable=True),
    sa.Column('goal_cpc', sa.Float(), nullable=True),
    sa.Column('client_monthly_budget', sa.Float(), nullable=True),
    sa.Column('client_total_budget', sa.Float(), nullable=True),
    sa.Column('google_monthly_spend', sa.Float(), nullable=True),
    sa.Column('google_total_spend', sa.Float(), nullable=True),
    sa.Column('goal_cpe', sa.Float(), nullable=True),
    sa.Column('monthly_events', sa.Float(), nullable=True),
    sa.Column('total_events', sa.Float(), nullable=True),
    sa.UniqueConstraint('order_id', 'external_id', name='uq_line_item_external'),
)



def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    # Creates only what is absent, so an already-built database is untouched.
    METADATA.create_all(bind, checkfirst=True)

    for table in METADATA.sorted_tables:
        if table.name not in existing:
            continue  # just created, so it is current by construction
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in have:
                op.add_column(table.name, _addable(column))

    for table in RETIRED_TABLES:
        if table in existing:
            op.drop_table(table)


# Values for the NOT NULL columns this baseline adds to tables that may
# already hold rows. Taken from the models' own defaults at that commit.
BACKFILL = {
    ("orders", "terms_locked"): False,
    ("orders", "active"): True,
    ("orders", "paused"): False,
    ("line_items", "terms_locked"): False,
    ("line_items", "sort_order"): 0,
    ("ingested_files", "kind"): "delivery",
    ("ingested_files", "status"): "ok",
}


def _addable(column: sa.Column) -> sa.Column:
    """A copy of a column that can be added to a table that already has rows.

    A NOT NULL column cannot be added to a populated table without a server
    default, so one is supplied for the columns that need it.
    """
    # A column that already declares one keeps it.
    server_default = column.server_default
    if server_default is None and not column.nullable:
        value = BACKFILL.get((column.table.name, column.name))
        if isinstance(value, bool):
            dialect = op.get_bind().dialect.name
            server_default = sa.text(
                ("1" if value else "0") if dialect == "sqlite"
                else ("true" if value else "false")
            )
        elif isinstance(value, (int, float)):
            server_default = sa.text(str(value))
        elif isinstance(value, str):
            server_default = sa.text(f"'{value}'")

    return sa.Column(
        column.name, column.type, nullable=column.nullable,
        server_default=server_default,
    )


def downgrade() -> None:
    """Not meaningful: this revision adopts an existing schema rather than
    creating one, so there is no single prior state to return to."""
    raise NotImplementedError("the adoption baseline cannot be downgraded")
