"""Ingest and order-book behaviour, on a slice shaped like the real export."""
from __future__ import annotations

import io

import os

import pandas as pd
import pytest

from ingest.normalize import file_date, normalize
from orderbook import is_paceable, line_item_label, never_ran, pacing_type_for

HEADER = (
    "business_unit,client,impressions,clicks,ctr,internal_cpm,internal_cost,"
    "goal_cpm_,goal_cpm_,goal_internal_cpm,campaign_name,campaign_id,"
    "avg_daily_serve,campaign_start_date,data_source_name,date,line_item_name,"
    "line_item_id,number_of_days_served,number_of_products,order_id,"
    "order_level_name,product,product_level_name,product_line_item_id,restricted,"
    "strategy_id,strategy_name,strategy_type,total_conversions,viewthroughs,"
    "click_conversions"
)


def frame(*rows: str) -> pd.DataFrame:
    return pd.read_csv(io.StringIO("\n".join([HEADER, *rows])), dtype=str, low_memory=False)


def row(**over) -> str:
    values = {
        "business_unit": "7 Mountains KY", "client": "Service One Credit Union",
        "impressions": "523", "clicks": "4", "ctr": "0.0", "internal_cpm": "3.42",
        "internal_cost": "1.79", "goal_cpm_a": "", "goal_cpm_b": "3.42",
        "goal_internal_cpm": "5.00", "campaign_name": "Service One #52753",
        "campaign_id": "120251183697710548", "avg_daily_serve": "523",
        "campaign_start_date": "2026-08-13", "data_source_name": "Facebook & Instagram Targeting",
        "date": "2026-08-22", "line_item_name": "Service One - Facebook/Instagram",
        "line_item_id": "126397", "number_of_days_served": "1", "number_of_products": "1",
        "order_id": "52753", "order_level_name": "Service One Credit Union #52753",
        "product": "Meta", "product_level_name": "Service One #52753",
        "product_line_item_id": "120251183697710548", "restricted": "",
        "strategy_id": "120251183697700548", "strategy_name": "Service One - Facebook/Instagram",
        "strategy_type": "Behavioral", "total_conversions": "0", "viewthroughs": "0",
        "click_conversions": "0",
    }
    values.update(over)
    return ",".join(str(values[k]) for k in values)


def test_duplicate_header_columns_do_not_break_the_read():
    """The export ships two columns both called `goal_cpm_`."""
    out = normalize(frame(row()))
    assert len(out) == 1
    # The one we keep is `goal_internal_cpm`, not either ambiguous column.
    assert out.iloc[0]["goal_cpm"] == 5.00


def test_rows_on_the_same_grain_are_summed_not_dropped():
    """A strategy running two creatives lands as two rows for one day."""
    out = normalize(frame(row(impressions="100", clicks="2"),
                          row(impressions="250", clicks="3")))
    assert len(out) == 1
    assert out.iloc[0]["impressions"] == 350
    assert out.iloc[0]["clicks"] == 5


def test_large_campaign_ids_keep_every_digit():
    """A 17-digit Meta id read as float64 loses its tail."""
    out = normalize(frame(row()))
    assert out.iloc[0]["campaign_id"] == "120251183697710548"


def test_ids_that_arrive_as_floats_are_cleaned():
    out = normalize(frame(row(order_id="52753.0")))
    assert out.iloc[0]["external_order_id"] == "52753"


def test_a_strategy_with_no_id_still_lands_on_its_own_row():
    """Adlib rows carry no strategy id; two strategies must not collapse."""
    out = normalize(frame(
        row(strategy_id="", strategy_name="Client - Retargeting Audio"),
        row(strategy_id="", strategy_name="Client - Behavioral Audio"),
    ))
    assert len(out) == 2
    assert all(sid.startswith("name:") for sid in out["strategy_id"])


def test_an_order_with_no_id_falls_back_to_its_name():
    out = normalize(frame(row(order_id="", order_level_name="Grand Home - CTV BETA")))
    assert out.iloc[0]["campaign_id"] == "120251183697710548"
    assert out.iloc[0]["external_order_id"] == ""


def test_unparseable_dates_are_dropped_rather_than_stored_as_null():
    out = normalize(frame(row(date="2026-08-22"), row(date="not a date")))
    assert len(out) == 1


def test_missing_date_column_is_an_error():
    bad = pd.DataFrame({"client": ["x"], "impressions": ["1"]})
    with pytest.raises(ValueError, match="date"):
        normalize(bad)


def test_file_date_reads_the_snapshot_day_from_the_key():
    import datetime as dt

    assert file_date("orders/client-serve_20260921_1202_0.csv") == dt.date(2026, 9, 21)
    assert file_date("orders/no-date-here.csv") is None


# --- order book classification --------------------------------------------
def test_ppc_and_linkedin_pace_on_clicks():
    assert pacing_type_for("PPC", "Google Ads Search") == "click"
    assert pacing_type_for("LinkedIn", "LinkedIn Targeting") == "click"


def test_performance_max_paces_on_events():
    assert pacing_type_for("PMax", "Google Ads Performance Max") == "event"


def test_everything_else_paces_on_impressions():
    assert pacing_type_for("CTV", "Amazon DSP") == "impression"
    assert pacing_type_for(None, None) == "impression"


def test_line_item_label_reads_like_the_hand_kept_rows():
    assert line_item_label("Social Mirror CTV", "Retargeting", None) == "SM CTV - Retargeting"
    assert line_item_label("Display", "Behavioral", None) == "D - Behavioral"
    assert line_item_label("Meta", None, "Client - Category Facebook", "Client") == "FB - Category Facebook"


def test_only_insertion_orders_are_paced():
    assert is_paceable("Insertion Order")
    assert is_paceable("insertion order")
    assert not is_paceable("Estimate")
    assert not is_paceable(None)


def test_a_cancelled_order_is_not_treated_as_never_having_run():
    """It may have delivered for months before it was cancelled."""
    assert not never_ran("Cancelled")
    assert never_ran("Draft")
    assert never_ran("Declined")


def test_delivery_carries_the_line_item_id_it_joins_on():
    out = normalize(frame(row(line_item_id="126397")))
    assert out.iloc[0]["external_line_item_id"] == "126397"


def test_a_line_item_with_no_id_gets_a_key_of_its_own():
    """Blank ids would otherwise collapse hundreds of orders into one."""
    out = normalize(frame(
        row(line_item_id="", line_item_name="Client - Audio"),
        row(line_item_id="", line_item_name="Client - CTV", strategy_id="s2"),
    ))
    keys = set(out["external_line_item_id"])
    assert len(keys) == 2
    assert all(k.startswith("name:Client - ") for k in keys)


def test_identifiers_stay_inside_the_column_they_have_to_fit():
    """The id columns are varchar(64) and the feed does not respect that.

    `line_item_id` sometimes carries a name rather than an id, and a key
    built from a strategy name runs to 130 characters. Postgres rejects the
    row; SQLite silently keeps it, so this only showed up against the real
    engine.
    """
    from ingest.normalize import bound_id

    long_name = "Clearfield Jefferson Drug and Alcohol Commission - Clearfield County Health"
    out = normalize(frame(
        row(line_item_id=long_name, strategy_id="", strategy_name=long_name),
    ))
    for column in ("campaign_id", "strategy_id", "external_order_id",
                   "external_line_item_id"):
        assert len(out.iloc[0][column]) <= 64, column

    # Short, real ids are untouched, so the join to the orders file still works.
    assert bound_id("126397") == "126397"


def test_two_long_names_sharing_an_opening_still_get_different_keys():
    from ingest.normalize import bound_id

    a = "A" * 60 + "-first"
    b = "A" * 60 + "-second"
    assert bound_id(a) != bound_id(b)
    assert len(bound_id(a)) <= 64


def test_a_grain_split_across_chunks_is_summed_not_overwritten(tmp_path):
    """The reason loads go through a staging table.

    A strategy running several creatives makes several rows for one day. Read
    in chunks and aggregated per chunk, the second chunk's upsert would
    replace the first chunk's total instead of adding to it - an undercount
    that only appears on files big enough to chunk.
    """
    import subprocess
    import sys

    from sqlalchemy import create_engine, func, select

    from models import DailyDelivery

    # Six rows on one grain, which a chunk size of 4 necessarily splits.
    csv = "\n".join([HEADER] + [row(impressions="100", clicks="1")] * 6)
    path = tmp_path / "client-serve_20260101_0000_0.csv"
    path.write_text(csv)

    url = f"sqlite:///{tmp_path}/m.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True,
    )

    import importlib

    import config
    import db as db_module

    os.environ["DATABASE_URL"] = url
    config.get_settings.cache_clear()
    importlib.reload(db_module)
    from ingest import loader

    importlib.reload(loader)
    loader.CHUNK_ROWS = 4

    with db_module.session_scope() as session:
        written, _ = loader.load_delivery_file(session, str(path), "test")

    engine = create_engine(url)
    with engine.connect() as conn:
        total = conn.execute(select(func.sum(DailyDelivery.__table__.c.impressions))).scalar()
        rows = conn.execute(select(func.count()).select_from(DailyDelivery.__table__)).scalar()

    assert written == 1
    assert rows == 1
    assert total == 600, "all six rows must be summed, not just the last chunk's"
