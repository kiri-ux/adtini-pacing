"""Ingest and order-book behaviour, on a slice shaped like the real export."""
from __future__ import annotations

import io

import pandas as pd
import pytest

from ingest.normalize import file_date, normalize
from orderbook import line_item_label, order_key, pacing_type_for

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


def test_order_key_prefers_the_order_number():
    class Row:
        external_order_id = "52753"
        order_level_name = "Service One Credit Union #52753"
        line_item_name = "x"

    assert order_key(Row()) == ("52753", "Service One Credit Union #52753")


def test_order_key_falls_back_to_the_name_for_beta_orders():
    class Row:
        external_order_id = None
        order_level_name = "Grand Home Furnishings - Premium CTV - BETA v2"
        line_item_name = "x"

    external, name = order_key(Row())
    assert external == ""
    assert name == "Grand Home Furnishings - Premium CTV - BETA v2"


# --- labels ---------------------------------------------------------------
def test_label_drops_the_client_prefix_the_feed_adds():
    from orderbook import strip_client_prefix

    assert strip_client_prefix(
        "Chalfant Corporation - Volkswagen of Boise - Facebook/Instagram Premium",
        "Chalfant Corporation - Volkswagen of Boise",
    ) == "Facebook/Instagram Premium"


def test_label_keeps_the_whole_targeting_not_just_its_tail():
    """`Premium` and `Premium Retargeting` are different rows."""
    client = "Chalfant Corporation - Volkswagen of Boise"
    plain = line_item_label("Meta", None, f"{client} - Facebook/Instagram Premium", client)
    retarget = line_item_label(
        "Meta", None, f"{client} - Facebook/Instagram Premium Retargeting", client
    )
    assert plain != retarget
    assert plain == "FB - Facebook/Instagram Premium"
    assert retarget == "FB - Facebook/Instagram Premium Retargeting"


def test_label_does_not_repeat_a_product_code_already_in_the_name():
    assert line_item_label("PPC", None, "Client - PPC Keywords", "Client") == "PPC Keywords"


def test_two_strategies_sharing_a_name_get_told_apart():
    from orderbook import unique_label

    taken: set[str] = set()
    assert unique_label("FB - Premium", taken) == "FB - Premium"
    assert unique_label("FB - Premium", taken) == "FB - Premium (2)"
    assert unique_label("FB - Premium", taken) == "FB - Premium (3)"
