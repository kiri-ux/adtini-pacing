"""Database schema.

Two halves that meet in the pacing engine:

* the **order book** (`Client` / `Order` / `LineItem`) - what was sold.
  Imported from the `orders*` drops, with `terms_locked` marking anything a
  buyer has since adjusted by hand so the next import leaves it alone.
* the **delivery feed** (`DailyDelivery` / `IngestedFile`) - what actually
  ran. Rebuilt from the `client-serve*` drops, never hand-edited.

Both kinds of drop land in the same S3 prefix and are told apart by their
filename.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Pacing types, matching the three tabs the buying team keeps by hand.
PACING_IMPRESSION = "impression"
PACING_CLICK = "click"
PACING_EVENT = "event"
PACING_TYPES = (PACING_IMPRESSION, PACING_CLICK, PACING_EVENT)


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    market: Mapped[str | None] = mapped_column(String(200))
    buyer: Mapped[str | None] = mapped_column(String(120))
    container_tag: Mapped[bool | None] = mapped_column(Boolean)

    orders: Mapped[list["Order"]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("client_id", "name", name="uq_order_client_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    # Adtini order number, e.g. "52753". Blank for Adlib/beta orders.
    external_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(400))
    pacing_type: Mapped[str] = mapped_column(String(20), default=PACING_IMPRESSION)
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    buyer: Mapped[str | None] = mapped_column(String(120))
    # Straight from the orders file. Only Insertion Orders get a pacing page;
    # a Cancelled order may still have run before it was cancelled, so it is
    # kept and shown when it has delivery rather than dropped on sight.
    order_type: Mapped[str | None] = mapped_column(String(60), index=True)
    status: Mapped[str | None] = mapped_column(String(60), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    last_adjusted_on: Mapped[dt.date | None] = mapped_column(Date)
    adjustment_note: Mapped[str | None] = mapped_column(String(300))
    # A buyer has adjusted this order's dates or pacing type, so the orders
    # import must not put them back. Mid-flight changes are the normal case,
    # not the exception.
    terms_locked: Mapped[bool] = mapped_column(Boolean, default=False)

    client: Mapped[Client] = relationship(back_populates="orders")
    line_items: Mapped[list["LineItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class LineItem(Base):
    """One line item on an order - a product, with what was sold on it.

    This is the grain the orders file works in, and the grain the sheet's
    "Campaign Elements" rows are read at. The delivery feed is finer (one row
    per strategy within the line item), so several strategies roll up here.

    Sold amounts are stored per pacing type. Only the block matching the
    order's `pacing_type` is read by the engine; the others stay null.
    """

    __tablename__ = "line_items"
    __table_args__ = (
        UniqueConstraint("order_id", "external_id", name="uq_line_item_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    name: Mapped[str] = mapped_column(String(400))
    product: Mapped[str | None] = mapped_column(String(120))
    strategy_type: Mapped[str | None] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # The orders file's own line item id. Delivery joins to it, and the next
    # import recognises the row by it rather than by its name.
    external_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # Sold terms here were edited by hand; the import leaves them alone.
    terms_locked: Mapped[bool] = mapped_column(Boolean, default=False)

    # Flight dates default to the order's when null.
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)

    # --- impression pacing ---
    monthly_impressions: Mapped[float | None] = mapped_column(Float)
    total_impressions: Mapped[float | None] = mapped_column(Float)
    goal_cpm: Mapped[float | None] = mapped_column(Float)
    # Where `goal_cpm` came from: "rate card", "orders file" or "buyer".
    # Three different CPMs exist for one line item and only the setup rate is
    # the one to pace on, so the page says which is in use.
    goal_cpm_source: Mapped[str | None] = mapped_column(String(20))
    # Restricted categories carry their own, higher, rate card entry.
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- click pacing (PPC, LinkedIn) ---
    monthly_spend: Mapped[float | None] = mapped_column(Float)
    total_spend: Mapped[float | None] = mapped_column(Float)
    goal_cpc: Mapped[float | None] = mapped_column(Float)

    # --- event pacing (Performance Max) ---
    client_monthly_budget: Mapped[float | None] = mapped_column(Float)
    client_total_budget: Mapped[float | None] = mapped_column(Float)
    google_monthly_spend: Mapped[float | None] = mapped_column(Float)
    google_total_spend: Mapped[float | None] = mapped_column(Float)
    goal_cpe: Mapped[float | None] = mapped_column(Float)
    monthly_events: Mapped[float | None] = mapped_column(Float)
    total_events: Mapped[float | None] = mapped_column(Float)

    order: Mapped[Order] = relationship(back_populates="line_items")


class DailyDelivery(Base):
    """One day of delivery for one strategy, aggregated on ingest."""

    __tablename__ = "daily_delivery"
    __table_args__ = (
        UniqueConstraint(
            "date", "data_source", "campaign_id", "strategy_id", name="uq_delivery_grain"
        ),
        Index("ix_delivery_client_date", "client_name", "date"),
        Index("ix_delivery_order_date", "external_order_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    data_source: Mapped[str] = mapped_column(String(120))
    campaign_id: Mapped[str] = mapped_column(String(64))
    strategy_id: Mapped[str] = mapped_column(String(64))

    business_unit: Mapped[str | None] = mapped_column(String(200))
    client_name: Mapped[str | None] = mapped_column(String(300))
    external_order_id: Mapped[str | None] = mapped_column(String(64))
    # The orders file's line item id. This plus `external_order_id` is how a
    # day of delivery finds the line item it was sold under.
    external_line_item_id: Mapped[str | None] = mapped_column(String(64), index=True)
    order_level_name: Mapped[str | None] = mapped_column(String(400))
    line_item_name: Mapped[str | None] = mapped_column(String(400))
    strategy_name: Mapped[str | None] = mapped_column(String(400))
    strategy_type: Mapped[str | None] = mapped_column(String(120))
    product: Mapped[str | None] = mapped_column(String(120))
    restricted: Mapped[str | None] = mapped_column(String(10))
    campaign_name: Mapped[str | None] = mapped_column(String(400))
    campaign_start_date: Mapped[dt.date | None] = mapped_column(Date)

    impressions: Mapped[float] = mapped_column(Float, default=0.0)
    clicks: Mapped[float] = mapped_column(Float, default=0.0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    conversions: Mapped[float] = mapped_column(Float, default=0.0)
    viewthroughs: Mapped[float] = mapped_column(Float, default=0.0)
    click_conversions: Mapped[float] = mapped_column(Float, default=0.0)
    goal_cpm: Mapped[float | None] = mapped_column(Float)

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DeliveryStaging(Base):
    """Landing table for a delivery file, before it is aggregated.

    A drop is read in chunks so it never sits in memory whole, but the same
    (date, source, campaign, strategy) can straddle a chunk boundary - a
    strategy running several creatives produces several rows for one day.
    Aggregating per chunk and upserting would let the second chunk overwrite
    the first's total instead of adding to it, quietly undercounting.

    So chunks land here unaggregated and the sum is done once, in the
    database, on the way into `daily_delivery`. Emptied either side of a load;
    it holds nothing between runs.
    """

    __tablename__ = "delivery_staging"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date)
    data_source: Mapped[str] = mapped_column(String(120))
    campaign_id: Mapped[str] = mapped_column(String(64))
    strategy_id: Mapped[str] = mapped_column(String(64))

    business_unit: Mapped[str | None] = mapped_column(String(200))
    client_name: Mapped[str | None] = mapped_column(String(300))
    external_order_id: Mapped[str | None] = mapped_column(String(64))
    external_line_item_id: Mapped[str | None] = mapped_column(String(64))
    order_level_name: Mapped[str | None] = mapped_column(String(400))
    line_item_name: Mapped[str | None] = mapped_column(String(400))
    strategy_name: Mapped[str | None] = mapped_column(String(400))
    strategy_type: Mapped[str | None] = mapped_column(String(120))
    product: Mapped[str | None] = mapped_column(String(120))
    restricted: Mapped[str | None] = mapped_column(String(10))
    campaign_name: Mapped[str | None] = mapped_column(String(400))
    campaign_start_date: Mapped[dt.date | None] = mapped_column(Date)

    impressions: Mapped[float] = mapped_column(Float, default=0.0)
    clicks: Mapped[float] = mapped_column(Float, default=0.0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    conversions: Mapped[float] = mapped_column(Float, default=0.0)
    viewthroughs: Mapped[float] = mapped_column(Float, default=0.0)
    click_conversions: Mapped[float] = mapped_column(Float, default=0.0)
    goal_cpm: Mapped[float | None] = mapped_column(Float)


class IngestedFile(Base):
    """Ingest log, so a re-run skips files already loaded."""

    __tablename__ = "ingested_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    s3_key: Mapped[str] = mapped_column(String(600), unique=True, index=True)
    # "delivery" or "orders", decided by the filename.
    kind: Mapped[str] = mapped_column(String(20), default="delivery")
    etag: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    rows_read: Mapped[int | None] = mapped_column(Integer)
    rows_written: Mapped[int | None] = mapped_column(Integer)
    min_date: Mapped[dt.date | None] = mapped_column(Date)
    max_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="ok")
    message: Mapped[str | None] = mapped_column(Text)
    # Columns the file carried that the importer did not recognise. Surfaced
    # on the Data page so a changed export is noticed rather than silently
    # half-read.
    unmapped_columns: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
