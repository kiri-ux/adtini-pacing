"""An order the search matched and the filters dropped.

Four filters on the overview drop an order without leaving a trace, and two
of them key off values the orders export changes under you. An order worked
on all week stops appearing and nothing anywhere says where it went.
"""
from __future__ import annotations

import datetime as dt
import importlib
import os
import subprocess
import sys

import pytest


@pytest.fixture()
def site(tmp_path):
    url = f"sqlite:///{tmp_path}/hidden.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True,
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
        client = Client(name="Ram Jack of Eastern Virginia")
        session.add(client)
        session.flush()
        for external_id, name, kind, status, end, active in (
            ("44807", "Live", "Insertion Order", "IO Live", dt.date(2026, 12, 31), True),
            ("44808", "Done", "Insertion Order", "IO Complete", dt.date(2026, 5, 31), True),
            ("44809", "Called off", "Insertion Order", "Cancelled", dt.date(2026, 12, 31), True),
            ("44810", "A quote", "Proposal", "Approved", dt.date(2026, 12, 31), True),
            ("44811", "Never ran", "Insertion Order", "Draft", dt.date(2026, 12, 31), False),
        ):
            order = Order(
                client_id=client.id, external_order_id=external_id,
                name=name, pacing_type="impression", order_type=kind,
                status=status, active=active,
                start_date=dt.date(2026, 1, 1), end_date=end,
            )
            session.add(order)
            session.flush()
            session.add(
                LineItem(
                    order_id=order.id, external_id=f"L{external_id}",
                    name="Display Ads", product="Display Ads", sort_order=0,
                    monthly_impressions=100_000.0, total_impressions=1_200_000.0,
                    goal_cpm=2.0,
                )
            )

    app_module.app.config["TESTING"] = True
    return app_module


def _hidden(query, **kw):
    import db as db_module
    import views

    with db_module.session_scope() as session:
        rows = views.overview(
            session, as_of=dt.date(2026, 9, 21), query=query, **kw
        )
        return {
            h.order.external_order_id: h.reason
            for h in views.hidden_matches(
                session, query, shown={r.order.id for r in rows},
                as_of=dt.date(2026, 9, 21), **kw
            )
        }, [r.order.external_order_id for r in rows]


def test_the_page_says_which_orders_it_is_not_showing(site):
    hidden, shown = _hidden("ram jack")

    assert shown == ["44807"], "only the live one is listed"
    assert hidden == {
        "44808": "ended 31 May 2026",
        "44809": "cancelled, nothing delivered",
        "44810": "Proposal",
        "44811": "Draft",
    }


def test_an_order_is_found_by_its_id(site):
    """The id was not searched, so typing the number off an order page found
    nothing and the only way back was a link you already had."""
    hidden, shown = _hidden("44808")

    assert shown == []
    assert hidden == {"44808": "ended 31 May 2026"}


def test_nothing_is_reported_hidden_once_the_filter_is_off(site):
    hidden, shown = _hidden("ram jack", include_ended=True)

    assert "44808" in shown
    assert "44808" not in hidden


def test_a_search_that_matches_nothing_reports_nothing(site):
    assert _hidden("some other client") == ({}, [])


def test_an_empty_search_reports_nothing(site):
    """Every order on the book is not a list of things gone missing."""
    assert _hidden("")[0] == {}


def test_the_bar_renders_with_a_link_to_each_one(site):
    app_module = site
    body = app_module.app.test_client().get(
        "/?q=ram+jack", follow_redirects=True
    ).get_data(as_text=True)

    assert "4 hidden" in body
    assert "ended 31 May 2026" in body
    assert "cancelled, nothing delivered" in body
