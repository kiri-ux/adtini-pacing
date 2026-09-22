"""Attaching a DSP campaign to a line item by hand.

Most delivery finds its line item on the ids the two exports share. Where it
does not, the line item reads as having served nothing at all - not
"under-pacing", but invisible - and a link is how a buyer says what belongs
to what.

These go through the real Flask app against a real migrated database rather
than calling the view functions, because the ways this has broken before were
route wiring and template rendering, which calling a function proves nothing
about.
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
    """The app, its database migrated to head, and one order to work on."""
    url = f"sqlite:///{tmp_path}/link.db"
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

    from models import Client, DailyDelivery, LineItem, Order

    with db_module.session_scope() as session:
        client = Client(name="W&L Subaru", market="7 Mountains PA")
        session.add(client)
        session.flush()
        order = Order(
            client_id=client.id,
            external_order_id="27900",
            name="W&L Subaru #27900",
            pacing_type="impression",
            order_type="Insertion Order",
            status="IO Live",
            start_date=dt.date(2026, 8, 1),
            end_date=dt.date(2026, 10, 31),
        )
        session.add(order)
        session.flush()
        # Sold, but the campaign that ran carries a line item id the orders
        # file never saw - which is the whole case for linking.
        session.add(
            LineItem(
                order_id=order.id,
                external_id="27919",
                name="Meta Display & Video Ads",
                product="Meta Display & Video Ads",
                monthly_impressions=90_000.0,
                total_impressions=270_000.0,
                goal_cpm=9.0,
            )
        )
        for day in range(1, 11):
            session.add(
                DailyDelivery(
                    date=dt.date(2026, 8, day),
                    data_source="Meta",
                    campaign_id="CMP-777",
                    strategy_id=f"S-{day}",
                    client_name="W&L Subaru",
                    external_order_id="27900",
                    external_line_item_id="NOT-27919",
                    campaign_name="W&L Subaru | Meta | Aug",
                    product="Meta Display & Video Ads",
                    strategy_name="Behavioral",
                    impressions=1_000.0,
                    clicks=10.0,
                    cost=9.0,
                )
            )
        order_id = order.id

    app_module.app.config["TESTING"] = True
    return app_module, order_id


def _line_item_id(app_module, order_id):
    from models import LineItem

    import db as db_module

    with db_module.session_scope() as session:
        return (
            session.query(LineItem)
            .filter(LineItem.order_id == order_id)
            .order_by(LineItem.id)
            .first()
            .id
        )


def _served(order_id) -> float:
    """What the order's pacing actually reads, not what the page mentions.

    The page names the candidate campaign's impressions in the picker too, so
    searching the HTML for the number proves nothing about whether it counted.
    """
    import db as db_module
    import views

    with db_module.session_scope() as session:
        view = views.order_view(session, order_id, as_of=dt.date(2026, 8, 10))
        return view.total.to_date


def test_delivery_that_matches_nothing_is_called_out(site):
    """Before any link: the page has to say it cannot see the delivery.

    Showing 0 served with no explanation is the failure mode - it reads as a
    campaign that is not running, when in fact it is running fine and the
    tool cannot find it.
    """
    app_module, order_id = site
    page = app_module.app.test_client().get(f"/orders/{order_id}").get_data(as_text=True)

    assert page.count("Not matched") >= 1
    # The campaign is there to be picked, with its numbers.
    assert "W&amp;L Subaru | Meta | Aug" in page


def test_linking_a_campaign_makes_its_delivery_count(site):
    """The point of the whole feature."""
    app_module, order_id = site
    client = app_module.app.test_client()
    line_item_id = _line_item_id(app_module, order_id)

    assert _served(order_id) == 0, "nothing should reach this line item yet"

    response = client.post(
        f"/orders/{order_id}/link",
        data={
            "line_item_id": str(line_item_id),
            "campaign": "Meta␟CMP-777",
            "ops_verified": "on",
            "linked_by": "Kiri",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    assert _served(order_id) == 10_000, (
        "the linked campaign's delivery must now count"
    )

    after = response.get_data(as_text=True)
    assert "Verified" in after
    assert "Kiri" in after


def test_a_campaign_belongs_to_one_line_item(site):
    """Linking a campaign that is already linked moves it, never copies it.

    Two line items both reading one campaign would count its impressions
    twice on the same order, which is worse than reading zero.
    """
    from models import CampaignLink, LineItem

    import db as db_module

    app_module, order_id = site
    client = app_module.app.test_client()
    first = _line_item_id(app_module, order_id)

    with db_module.session_scope() as session:
        second = LineItem(
            order_id=order_id, name="Second", product="Display Ads", sort_order=1
        )
        session.add(second)
        session.flush()
        second_id = second.id

    for target in (first, second_id):
        client.post(
            f"/orders/{order_id}/link",
            data={"line_item_id": str(target), "campaign": "Meta␟CMP-777"},
            follow_redirects=True,
        )

    with db_module.session_scope() as session:
        links = session.query(CampaignLink).all()
        assert len(links) == 1
        assert links[0].line_item_id == second_id


def test_unlinking_puts_the_delivery_back_out_of_reach(site):
    app_module, order_id = site
    client = app_module.app.test_client()
    line_item_id = _line_item_id(app_module, order_id)

    client.post(
        f"/orders/{order_id}/link",
        data={"line_item_id": str(line_item_id), "campaign": "Meta␟CMP-777"},
        follow_redirects=True,
    )
    page = client.post(
        f"/orders/{order_id}/link/{line_item_id}/clear", follow_redirects=True
    ).get_data(as_text=True)

    assert "Not matched" in page
    assert _served(order_id) == 0


def test_the_overview_counts_linked_delivery_too(site):
    """A link is not a detail-page trick - it changes what the order is."""
    app_module, order_id = site
    client = app_module.app.test_client()
    line_item_id = _line_item_id(app_module, order_id)

    client.post(
        f"/orders/{order_id}/link",
        data={"line_item_id": str(line_item_id), "campaign": "Meta␟CMP-777"},
        follow_redirects=True,
    )
    import db as db_module
    import views

    with db_module.session_scope() as session:
        row = next(
            r for r in views.overview(session, as_of=dt.date(2026, 8, 10))
            if r.order.id == order_id
        )
    assert row.total.to_date == 10_000

    # And it renders.
    assert client.get("/?as_of=2026-08-10").status_code == 200


def test_every_page_renders(site):
    """A blunt smoke test, because the ways this app has broken were 500s.

    A template that references something the view does not pass raises only
    when the page is actually rendered, and nothing else here renders them.
    """
    app_module, order_id = site
    client = app_module.app.test_client()

    for path in ("/", "/data", f"/orders/{order_id}", "/healthz"):
        assert client.get(path).status_code == 200, path


# --- a stray NaN must never take a page down -------------------------------
def test_a_nan_sold_term_does_not_break_the_order_page(site):
    """What the live site did: "cannot convert float NaN to integer".

    `entry` called `int(value)` to decide whether to show decimals, and
    `int(nan)` raises - so one damaged row took the whole order page with it,
    error screen and all. Formatting is the last place that should be able to
    decide a page cannot be shown.

    Written against Postgres behaviour rather than SQLite's: SQLite will not
    store a NaN, so the value is put on the object and rendered directly.
    """
    app_module, order_id = site
    from models import LineItem

    import db as db_module

    with db_module.session_scope() as session:
        item = (
            session.query(LineItem).filter(LineItem.order_id == order_id).first()
        )
        item.monthly_impressions = float("nan")
        item.total_impressions = float("nan")
        item.goal_cpm = float("nan")
        session.flush()

        # Rendered straight from the object, the way the live page saw it.
        page = app_module.app.jinja_env.from_string(
            "{{ v|entry }}|{{ v|num }}|{{ v|money }}|{{ v|pct }}|{{ v|paceclass }}"
        ).render(v=float("nan"))

    assert page == "|—|—|—|unknown", page
    assert app_module.app.test_client().get(f"/orders/{order_id}").status_code == 200
