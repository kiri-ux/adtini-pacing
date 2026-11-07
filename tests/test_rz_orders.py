"""Reading orders straight from RZ's order tool."""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ingest import rz_orders

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# --- mapping ---------------------------------------------------------------
def test_line_items_map_onto_export_fields():
    rows = rz_orders.rows_for(load("rz_order_56142.json")["order"], "QXO Goodrich")
    assert [r["external_line_item_id"] for r in rows] == ["136565", "136566"]
    mc = rows[0]
    assert mc["client_name"] == "QXO - Goodrich"
    assert mc["business_unit"] == "Cape Cod Broadcasting"
    assert mc["external_order_id"] == "56142"
    assert mc["order_name"] == "QXO Goodrich"
    assert mc["product"] == "Mobile Conquesting Display & Video Ads"
    assert mc["status"] == "IO Pending Approval"
    assert mc["item_status"] == "IO Pending Approval"
    assert mc["order_type"] == "Insertion Order"
    assert mc["buyer"] == "Alyssa Frank"
    assert mc["start_date"] == dt.date(2026, 11, 1)
    assert mc["end_date"] == dt.date(2027, 4, 30)
    assert mc["monthly_impressions"] == 164705
    assert mc["total_impressions"] == 988230
    assert mc["months_running"] == 6
    assert mc["total_campaign_budget"] == 16800
    assert mc["sold_strategies"] == "Category Targeting, Geo-Retargeting, Geo-Fencing"


def test_strategies_fall_back_to_the_plain_text():
    item = {"strategies": "Keyword Targeting, AI Targeting"}
    assert rz_orders.sold_strategies(item) == "Keyword Targeting, AI Targeting"
    assert rz_orders.sold_strategies({"display_strategies": []}) is None


def test_ad_spend_fields_are_read_when_present():
    order = {
        "id": "1", "client_id": ["9", "Acme"], "lineItems": [{
            "id": "2", "product_name": "Pay-Per-Click Ads",
            "total_ppc_ad_spend": "1,200", "monthly_ppc_ad_spend": "200",
        }],
    }
    row = rz_orders.rows_for(order)[0]
    assert row["total_ppc_spend"] == 1200
    assert row["monthly_ppc_spend"] == 200


def test_labels_and_blanks():
    assert rz_orders.label(["2", "Insertion Order"]) == "Insertion Order"
    assert rz_orders.label([None]) is None
    assert rz_orders.label([["1", "A"], ["2", "B"]]) == "A, B"
    assert rz_orders.date("0000-00-00") is None
    assert rz_orders.date("11-01-2026") == dt.date(2026, 11, 1)
    assert rz_orders.number("") is None


# --- session ---------------------------------------------------------------
class FakeRZ:
    """Answers like RZ: login sets a cookie, orders need it."""

    def __init__(self, orders: dict, expire_once: bool = False):
        self.orders = orders
        self.calls: list[str] = []
        self.logins = 0
        self.expire_once = expire_once

    def __call__(self, method, url, body, headers):
        path = url.replace(rz_orders.BASE, "")
        self.calls.append(path)
        if path == rz_orders.LOGIN_PATH:
            self.logins += 1
            payload = json.loads(body)
            ok = payload["password"] == "pw"
            data = {"data": {"settings": {"isLoggedIn": ok}}}
            return 200, [("Set-Cookie", f"sid=s{self.logins}; Path=/")], json.dumps(data).encode()
        if "sid=" not in headers.get("Cookie", ""):
            return 200, [], b'{"status":"redirect","message":"Please log in"}'
        if self.expire_once:
            self.expire_once = False
            return 200, [], b'{"status":"redirect","message":"Session expired"}'
        if path == rz_orders.OPEN_ORDERS_PATH:
            rows = [{"id": k, "order_name": f"Order {k}"} for k in self.orders]
            return 200, [], json.dumps(rows).encode()
        order_id = path.split("id=")[1]
        if order_id not in self.orders:
            return 500, [], b'{"error":"nope"}'
        return 200, [], json.dumps(self.orders[order_id]).encode()


def test_logs_in_once_and_relogs_when_the_session_expires():
    fake = FakeRZ({"56142": load("rz_order_56142.json")}, expire_once=True)
    client = rz_orders.RZClient("bot@example.com", "pw", transport=fake)
    order = client.order("56142")
    assert order["id"] == "56142"
    assert fake.logins == 2


def test_a_bad_password_says_so():
    client = rz_orders.RZClient("bot@example.com", "wrong", transport=FakeRZ({}))
    with pytest.raises(rz_orders.RZError, match="login failed"):
        client.open_orders()
    assert "wrong" not in repr(client)


# --- full sync into the order book ------------------------------------------
@pytest.fixture()
def db(tmp_path):
    url = f"sqlite:///{tmp_path}/rz.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True,
    )
    os.environ["DATABASE_URL"] = url
    import config

    config.get_settings.cache_clear()
    import db as db_module

    importlib.reload(db_module)
    return db_module


def test_sync_adds_new_orders_and_refreshes_recent_ones(db):
    from sqlalchemy import select

    from models import Client, IngestedFile, LineItem, Order

    # An order that ended last week, not in RZ's open list.
    ended = load("rz_order_55594.json")
    ended["order"]["status_name"] = "IO Complete"
    with db.session_scope() as session:
        client = Client(name="SBBL Law")
        session.add(client)
        session.flush()
        session.add(Order(
            client_id=client.id, external_order_id="55594", name="SBBL Law #55594",
            order_type="Insertion Order", status="IO Live",
            start_date=dt.date(2026, 8, 5), end_date=dt.date(2026, 11, 1),
        ))

    fake = FakeRZ({"56142": load("rz_order_56142.json"), "55594": ended})
    open_list = {"56142": fake.orders["56142"]}

    def transport(method, url, body, headers):
        if url.endswith(rz_orders.OPEN_ORDERS_PATH):
            fake.calls.append("open")
            rows = [{"id": k, "order_name": "QXO Goodrich"} for k in open_list]
            return 200, [], json.dumps(rows).encode()
        return fake(method, url, body, headers)

    client = rz_orders.RZClient("bot@example.com", "pw", transport=transport)
    result = rz_orders.sync(client, db.session_scope, today=dt.date(2026, 11, 7), pause=0)

    assert result.orders_seen == 2
    assert result.orders_read == 2
    assert result.errors == []
    assert result.orders_added == 1
    assert result.line_items_added == 3

    with db.session_scope() as session:
        new = session.execute(select(Order).where(Order.external_order_id == "56142")).scalar_one()
        assert new.name == "QXO Goodrich"
        assert new.status == "IO Pending Approval"
        assert new.buyer == "Alyssa Frank"
        items = {li.external_id: li for li in new.line_items}
        assert items["136566"].sold_strategies == "B2B Behavioral Targeting, Keyword Targeting, AI Targeting"
        assert items["136566"].monthly_impressions == 253333

        old = session.execute(select(Order).where(Order.external_order_id == "55594")).scalar_one()
        assert old.status == "IO Complete"
        assert old.end_date == dt.date(2027, 1, 31)

        log = session.execute(select(IngestedFile)).scalar_one()
        assert log.s3_key == "rz://orders/2026-11-07"
        assert log.status == "ok"


def test_one_failing_order_does_not_stop_the_rest(db):
    fake = FakeRZ({"56142": load("rz_order_56142.json")})

    def transport(method, url, body, headers):
        if url.endswith(rz_orders.OPEN_ORDERS_PATH) and "sid=" in headers.get("Cookie", ""):
            return 200, [], json.dumps([{"id": "56142"}, {"id": "99999"}]).encode()
        return fake(method, url, body, headers)

    client = rz_orders.RZClient("bot@example.com", "pw", transport=transport)
    result = rz_orders.sync(client, db.session_scope, today=dt.date(2026, 11, 7), pause=0)
    assert result.orders_read == 1
    assert len(result.errors) == 1 and "99999" in result.errors[0]
    assert result.line_items_added == 2
