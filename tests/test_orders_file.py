"""Reading the `orders*` export, which is a flattened join and shows it."""
from __future__ import annotations

import datetime as dt
import os
import io

import pandas as pd
import pytest

from ingest.loader import DELIVERY, ORDERS, classify
from ingest.orders import match_columns, normalize, simplify, strip_html
from orderbook import retail_cpm


def csv(text: str) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(text), dtype=str, low_memory=False)


HEAD = (
    "client_business_unit,orders_status,client,orders_id,product,id,status,"
    "orders_start_date,start_date,start_date,end_date,orders_end_date,"
    "monthly_campaign_impressions,total_campaign_impressions,"
    "total_campaign_impressions,total_campaign_budget,campaign_manager,order_type"
)


def line(**over) -> str:
    v = {
        "bu": "7 Mountains KY", "ostatus": "Approved", "client": "Service One",
        "oid": '"<a href=""/client/iotool/dist/#/items/viewOrder/52753"">52753</a>"',
        "product": "Display",
        "id": '"<a href=""/client/iotool/dist/#/items/viewLineItem/126397"">126397</a>"',
        "status": "Approved", "ostart": "2026-08-13 21:00:00", "start": "",
        "start2": "2026-08-13 21:00:00", "end": "2026-12-31 21:00:00",
        "oend": "2026-12-31 21:00:00", "monthly": "100000", "total": "",
        "total2": "700000", "budget": "3500.00",
        "manager": "Lauren Smith (lauren@vicimediainc.com)", "otype": "Insertion Order",
    }
    v.update(over)
    return ",".join(str(v[k]) for k in v)


# --- routing ---------------------------------------------------------------
def test_files_are_routed_by_their_filename():
    assert classify("orders/client-serve_20260921_1202_0.csv") == DELIVERY
    assert classify("orders/orders-db-inno_20260921_1853_0.csv") == ORDERS
    assert classify("orders/orders-db-conquest_20260921_1853_0.csv.zip") == ORDERS


def test_an_unrecognised_file_is_skipped_not_guessed_at():
    assert classify("orders/something-new_20260921.csv") is None


# --- the export's quirks ---------------------------------------------------
def test_ids_arrive_wrapped_in_html():
    assert strip_html('<a href="/x/viewOrder/2873">2873</a>') == "2873"
    assert strip_html("2873") == "2873"
    assert strip_html(None) is None


def test_repeated_headers_collapse_to_one_field():
    """`start_date` appears twice and pandas suffixes the second."""
    assert simplify("start_date") == simplify("start_date.1") == "startdate"


def test_a_field_reads_every_copy_of_its_column():
    mapped, _ = match_columns(
        ["client", "orders_id", "start_date", "start_date.1", "orders_start_date"]
    )
    assert mapped["start_date"] == ["orders_start_date", "start_date", "start_date.1"]


def test_the_value_is_taken_from_whichever_copy_has_it():
    """`start_date` is blank and `start_date.1` carries the date."""
    out = normalize(csv(HEAD + "\n" + line())).rows
    assert out.iloc[0]["start_date"] == dt.date(2026, 8, 13)
    assert out.iloc[0]["total_impressions"] == 700000


def test_dates_drop_the_time_the_export_carries():
    out = normalize(csv(HEAD + "\n" + line())).rows
    assert out.iloc[0]["end_date"] == dt.date(2026, 12, 31)


def test_the_buyer_loses_their_email():
    out = normalize(csv(HEAD + "\n" + line())).rows
    assert out.iloc[0]["buyer"] == "Lauren Smith"


def test_the_join_repeats_rows_and_they_collapse():
    """One line item arriving many times is one line item."""
    out = normalize(csv(HEAD + "\n" + line() + "\n" + line() + "\n" + line())).rows
    assert len(out) == 1


def test_two_line_items_on_one_order_stay_two():
    second = line(id='"<a href=""/x/viewLineItem/126398"">126398</a>"', product="CTV")
    out = normalize(csv(HEAD + "\n" + line() + "\n" + second)).rows
    assert len(out) == 2
    assert set(out["external_line_item_id"]) == {"126397", "126398"}


def test_a_cancelled_order_is_kept():
    """It may have run before it was cancelled, so it is not dropped here."""
    out = normalize(csv(HEAD + "\n" + line(ostatus="Cancelled", status="Cancelled"))).rows
    assert len(out) == 1
    assert out.iloc[0]["status"] == "Cancelled"


def test_unrecognised_columns_are_reported_not_silently_dropped():
    frame = normalize(csv(HEAD + ",brand_new_column\n" + line() + ",x"))
    assert "brand_new_column" in frame.unmapped
    assert "brand_new_column" in frame.unmapped_note


def test_a_file_with_no_client_column_is_an_error():
    with pytest.raises(ValueError, match="client_name"):
        normalize(csv("orders_id,product\n1,Display"))


def test_money_survives_its_formatting():
    from ingest.orders import _money

    assert _money("$23,814.00") == 23814.0
    assert _money("(1,234)") == -1234.0
    assert _money("—") is None
    assert _money("") is None


# --- deriving the rate -----------------------------------------------------
def test_retail_cpm_comes_from_budget_and_impressions():
    """What the client is billed - used for margin, never for pacing."""
    assert retail_cpm(3500.0, 700000.0) == 5.0
    assert retail_cpm(None, 700000.0) is None
    assert retail_cpm(3500.0, 0) is None


# --- schema ----------------------------------------------------------------
def test_the_app_never_builds_its_own_schema():
    """`create_all` creates missing tables but never alters existing ones.

    Running it against a live database leaves columns added later off and
    every query for one then fails - which is exactly what took the first
    deploy down. Alembic owns the schema; nothing in the request path may
    touch it.
    """
    import ast
    import pathlib

    banned = {"create_all", "init_db"}
    for name in ("app.py", "scripts/ingest.py", "views.py", "orderbook.py"):
        tree = ast.parse(pathlib.Path(name).read_text())
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else
            getattr(node.func, "id", None)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert not (called & banned), f"{name} must not build the schema"


def test_a_migrated_database_can_take_a_bulk_insert(tmp_path):
    """Regression: the frozen baseline dropped `updated_at`'s server default.

    The ingest inserts in bulk without setting it, so every row failed NOT
    NULL on a freshly migrated database - which no unit test touched, because
    they all build the schema from the models instead.
    """
    import datetime as dt
    import subprocess
    import sys

    url = f"sqlite:///{tmp_path}/m.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True,
    )

    from sqlalchemy import create_engine, insert, select
    from models import DailyDelivery

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            insert(DailyDelivery.__table__).values(
                date=dt.date(2026, 9, 1), data_source="x", campaign_id="1",
                strategy_id="1", impressions=1.0, clicks=0.0, cost=0.0,
                conversions=0.0, viewthroughs=0.0, click_conversions=0.0,
            )
        )
        stored = conn.execute(select(DailyDelivery.__table__)).first()
    assert stored.updated_at is not None


def test_the_sweep_does_not_run_inside_the_web_worker():
    """Render restarted the instance for exceeding its memory limit.

    A sweep is minutes of work and a couple of hundred megabytes. Run inside
    the request it took the whole service down, so the route must hand it to
    another process and return - never call the loader itself.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app.py").read_text())
    route = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "data_page"
    )
    called = {
        node.func.attr for node in ast.walk(route)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "run" not in called, "data_page must not run the sweep in-request"


def test_the_ingest_script_actually_runs(tmp_path):
    """It is the cron job's entrypoint and nothing imported it.

    Python puts the script's own directory on sys.path rather than the
    working directory, so `python scripts/ingest.py` could not see the app
    beside it - and the script still imported a function deleted two commits
    earlier. Both only show up by running it, which nothing did.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "scripts/ingest.py"],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "DATABASE_URL": f"sqlite:///{tmp_path}/m.db",
            # Deliberately unusable, so the run reaches the S3 call and stops
            # there rather than touching a real bucket.
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
        },
    )
    output = result.stdout + result.stderr
    assert "ModuleNotFoundError" not in output, output[-600:]
    assert "ImportError" not in output, output[-600:]
    assert "sweep starting" in output
