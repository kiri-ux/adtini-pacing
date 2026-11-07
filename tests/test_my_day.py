"""My Day: a buyer's partners for one weekday, from the update schedule."""
from __future__ import annotations

import datetime as dt
import importlib
import os
import subprocess
import sys

import pytest

import schedule

SHEET = (
    "Partner,#,Buyer,Email,SEO,,,Monday Markets,#,Buyer,,Tuesday Markets,#,Buyer,,"
    "Wednesday Markets,#,Buyer,,Thursday Markets,#,Buyer,,Friday Markets,#,Buyer\n"
    "Lotus Boise,11,Laura,laura@x.com,Matt,m@x.com,,Lotus Boise,11,Laura,,"
    "Manning Media,16,Mallory,,Other Co,1,Bree,,Western Rockies Radio,0,Mallory,,"
    '"LM Charleston, WV",0,Mallory\n'
    "Manning Media,16,Mallory,mal@x.com,Matt,m@x.com,,DigiRu,0,Mallory,,,,,,,,,,,,,,,,\n"
    ",,,,,,,Total Campaigns:,,142,,,,,,,,,,,,,,,,\n"
    ",,,,,,,Laura,,11,,,,,,,,,,,,,,,,\n"
)


def test_the_sheet_reads_each_day_down_to_its_totals():
    entries = schedule.parse(SHEET)
    got = {(e.market, schedule.WEEKDAYS[e.weekday], e.buyer) for e in entries}
    assert got == {
        ("Lotus Boise", "Monday", "Laura"),
        ("DigiRu", "Monday", "Mallory"),
        ("Manning Media", "Tuesday", "Mallory"),
        ("Other Co", "Wednesday", "Bree"),
        ("Western Rockies Radio", "Thursday", "Mallory"),
        ("LM Charleston, WV", "Friday", "Mallory"),
    }
    assert {e.buyer: e.email for e in entries}["Laura"] == "laura@x.com"


def test_a_sheet_with_the_wrong_columns_is_refused():
    with pytest.raises(ValueError):
        schedule.parse("Partner,#,Buyer\nA,1,B\n")


def test_the_snapshot_parses():
    entries = schedule.parse(schedule.SNAPSHOT.read_text(encoding="utf-8"))
    assert len(entries) > 250
    assert ("Lotus Boise", 0) in {(e.market, e.weekday) for e in entries if e.buyer == "Laura"}


def test_weekend_reads_as_monday():
    assert schedule.today_weekday(dt.date(2026, 11, 7)) == 0  # Saturday
    assert schedule.today_weekday(dt.date(2026, 11, 5)) == 3  # Thursday


@pytest.fixture()
def site(tmp_path):
    url = f"sqlite:///{tmp_path}/myday.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )
    os.environ["DATABASE_URL"] = url
    os.environ["APP_PASSWORD"] = ""

    import config

    config.get_settings.cache_clear()
    import db as db_module

    importlib.reload(db_module)
    import views
    import app as app_module

    importlib.reload(views)
    importlib.reload(app_module)

    from models import Client, LineItem, Order

    with db_module.session_scope() as session:
        schedule.load(session, SHEET)
        for name, market, number in (
            ("Peters", "Manning  Media", "51741"),
            ("Elsewhere", "Some Other Partner", "51742"),
            ("Hers Elsewhere", "Other Co", "51743"),
        ):
            client = Client(name=name, market=market)
            session.add(client)
            session.flush()
            order = Order(
                client_id=client.id, external_order_id=number, name=f"{name} #{number}",
                pacing_type="impression", order_type="Insertion Order", status="IO Live",
                start_date=dt.date(2026, 10, 1), end_date=dt.date(2027, 3, 31),
                buyer="Mallory Hoenig" if number == "51743" else "Bree Someone",
            )
            session.add(order)
            session.flush()
            session.add(LineItem(
                order_id=order.id, external_id=f"9{number}", name="MC",
                product="Mobile Conquesting Display & Video Ads",
                monthly_impressions=100_000.0, total_impressions=600_000.0, goal_cpm=4.5,
            ))

    app_module.app.config["TESTING"] = True
    return app_module


def test_my_day_lists_the_buyers_partners_for_that_day(site):
    body = site.app.test_client().get("/my-day?buyer=Mallory&day=1").get_data(as_text=True)
    assert "Peters" in body
    assert "Elsewhere" not in body


def test_a_partner_with_no_orders_is_marked(site):
    body = site.app.test_client().get("/my-day?buyer=Mallory&day=4").get_data(as_text=True)
    assert "LM Charleston, WV" in body
    assert "no orders" in body


def test_my_day_remembers_the_buyer(site):
    client = site.app.test_client()
    client.get("/my-day?buyer=Mallory&day=1")
    response = client.get("/my-day?day=1")
    assert response.status_code == 302
    assert "buyer=Mallory" in response.headers["Location"]


def test_home_lists_todays_buyers_and_the_rail_goes_there(site):
    client = site.app.test_client()
    body = client.get("/home").get_data(as_text=True)
    assert "My Day" in body
    overview = client.get("/").get_data(as_text=True)
    assert 'href="/home" title="Home"' in overview


def test_the_schedule_can_be_replaced_from_the_data_page(site):
    import io

    import db as db_module
    from models import ScheduleEntry

    sheet = SHEET.replace("DigiRu", "New Partner")
    site.app.test_client().post(
        "/data",
        data={"action": "schedule", "file": (io.BytesIO(sheet.encode()), "schedule.csv")},
        content_type="multipart/form-data",
    )
    with db_module.session_scope() as session:
        markets = {e.market for e in session.query(ScheduleEntry)}
    assert "New Partner" in markets and "DigiRu" not in markets


def test_the_buyers_own_orders_come_on_the_partners_day(site):
    # Other Co is on Bree's Wednesday, but this order is Mallory's.
    client = site.app.test_client()
    body = client.get("/my-day?buyer=Mallory&day=2").get_data(as_text=True)
    assert "Hers Elsewhere" in body
    body = client.get("/my-day?buyer=Mallory&day=1").get_data(as_text=True)
    assert "Hers Elsewhere" not in body


def test_schedule_names_map_to_order_buyers(site):
    import db as db_module

    with db_module.session_scope() as session:
        assert schedule.order_buyer(session, "Mallory") == "Mallory Hoenig"
        assert schedule.order_buyer(session, "Nobody") is None


def test_seeding_reads_back_in_the_same_session(site):
    import db as db_module
    from models import ScheduleEntry

    with db_module.session_scope() as session:
        session.query(ScheduleEntry).delete()
    with db_module.session_scope() as session:
        schedule.seed_if_empty(session)
        assert schedule.by_day(session, 0)


def test_the_warmer_builds_the_pilot_my_days(site):
    with site.app.test_request_context():
        paths = site._warm_paths()
    assert "/home" in paths
    assert "/my-day?buyer=Laura&day=0" in paths
    assert "/my-day?buyer=Mallory&day=4" in paths
