"""Database schema.

Two halves that meet in the pacing engine:

* the **order book** (`Client` / `Order` / `LineItem`) - what was sold. The
  buying team owns this; the delivery feed never contains sold totals or end
  dates, so it has to be maintained here.
* the **delivery feed** (`DailyDelivery` / `IngestedFile`) - what actually
  ran. Rebuilt from the S3 drops, never hand-edited.
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
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    last_adjusted_on: Mapped[dt.date | None] = mapped_column(Date)
    adjustment_note: Mapped[str | None] = mapped_column(String(300))

    client: Mapped[Client] = relationship(back_populates="orders")
    line_items: Mapped[list["LineItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class LineItem(Base):
    """One "Campaign Elements" row in the buying team's sheet.

    Sold amounts are stored per pacing type. Only the block matching the
    order's `pacing_type` is read by the engine; the others stay null.
    """

    __tablename__ = "line_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    name: Mapped[str] = mapped_column(String(400))
    product: Mapped[str | None] = mapped_column(String(120))
    strategy_type: Mapped[str | None] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    # Flight dates default to the order's when null.
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)

    # --- impression pacing ---
    monthly_impressions: Mapped[float | None] = mapped_column(Float)
    total_impressions: Mapped[float | None] = mapped_column(Float)
    goal_cpm: Mapped[float | None] = mapped_column(Float)

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
    mappings: Mapped[list["DeliveryMapping"]] = relationship(
        back_populates="line_item", cascade="all, delete-orphan"
    )


class DeliveryMapping(Base):
    """Ties a line item to the delivery rows that feed it.

    The feed's own identifiers are messy (null order ids, reused campaign
    ids), so matching is explicit rather than inferred at read time.
    """

    __tablename__ = "delivery_mappings"
    __table_args__ = (
        UniqueConstraint("data_source", "campaign_id", "strategy_id", name="uq_mapping_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    line_item_id: Mapped[int] = mapped_column(ForeignKey("line_items.id"), index=True)
    data_source: Mapped[str] = mapped_column(String(120))
    campaign_id: Mapped[str] = mapped_column(String(64))
    strategy_id: Mapped[str] = mapped_column(String(64))

    line_item: Mapped[LineItem] = relationship(back_populates="mappings")


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
    order_level_name: Mapped[str | None] = mapped_column(String(400))
    line_item_name: Mapped[str | None] = mapped_column(String(400))
    strategy_name: Mapped[str | None] = mapped_column(String(400))
    strategy_type: Mapped[str | None] = mapped_column(String(120))
    product: Mapped[str | None] = mapped_column(String(120))
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


class IngestedFile(Base):
    """Ingest log, so a re-run skips files already loaded."""

    __tablename__ = "ingested_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    s3_key: Mapped[str] = mapped_column(String(600), unique=True, index=True)
    etag: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    rows_read: Mapped[int | None] = mapped_column(Integer)
    rows_written: Mapped[int | None] = mapped_column(Integer)
    min_date: Mapped[dt.date | None] = mapped_column(Date)
    max_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="ok")
    message: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
