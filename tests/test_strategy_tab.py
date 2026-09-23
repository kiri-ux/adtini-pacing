"""The order page's three views, and the strategy split beneath them.

The split is the only thing that says which targeting to push when a product
is under-pacing: the orders export stops at the product line item and the
delivery feed only says what ran. It lives nowhere else, so it has to be
editable here - including adding targeting bought mid-flight that no export
will ever mention.
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
    url = f"sqlite:///{tmp_path}/strategy.db"
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

    from models import Client, DailyDelivery, LineItem, Order, StrategyTerms

    with db_module.session_scope() as session:
        client = Client(name="Bud's Auto")
        session.add(client)
        session.flush()
        order = Order(
            client_id=client.id,
            external_order_id="44100",
            name="Bud's Auto #44100",
            pacing_type="impression",
            order_type="Insertion Order",
            status="IO Live",
            start_date=dt.date(2026, 8, 1),
            end_date=dt.date(2027, 1, 31),
        )
        session.add(order)
        session.flush()

        meta = LineItem(
            order_id=order.id, external_id="88001", name="Meta",
            product="Meta Display & Video Ads", sort_order=0,
            monthly_impressions=128_000.0, total_impressions=1_536_000.0,
            goal_cpm=2.96,
        )
        # Sold in ad spend, on an order that otherwise paces impressions.
        ppc = LineItem(
            order_id=order.id, external_id="88002", name="PPC",
            product="Pay-Per-Click Ads", sort_order=1,
            pacing_type="click",
            monthly_spend=3_000.0, total_spend=18_000.0,
        )
        session.add_all([meta, ppc])
        session.flush()

        session.add_all([
            StrategyTerms(
                order_id=order.id, line_item_id=meta.id, label="FB - Retargeting",
                match_key="retargeting", monthly_target=10_000.0,
                total_target=120_000.0, rate=4.56, sort_order=0,
            ),
            StrategyTerms(
                order_id=order.id, label="FB - Categories",
                match_key="categories", monthly_target=118_000.0,
                total_target=1_416_000.0, rate=1.35, sort_order=1,
            ),
        ])

        for day in range(1, 21):
            for strategy, name, impressions in (
                ("S-RT", "Meta - Retargeting", 400.0),
                ("S-CT", "Meta - Categories", 3_800.0),
            ):
                session.add(
                    DailyDelivery(
                        date=dt.date(2026, 8, day),
                        data_source="Meta",
                        campaign_id="CMP-1",
                        strategy_id=f"{strategy}-{day}",
                        client_name="Bud's Auto",
                        external_order_id="44100",
                        external_line_item_id="88001",
                        product="Meta Display & Video Ads",
                        strategy_name=name,
                        strategy_type=name.split(" - ")[-1],
                        impressions=impressions,
                        clicks=4.0,
                        cost=impressions * 0.003,
                    )
                )
        order_id = order.id

    app_module.app.config["TESTING"] = True
    return app_module, order_id


def test_all_three_tabs_render(site):
    app_module, order_id = site
    client = app_module.app.test_client()
    for tab in ("order", "campaign", "strategy", "nonsense"):
        page = client.get(f"/orders/{order_id}?tab={tab}")
        assert page.status_code == 200, tab


def test_the_strategy_tab_paces_each_targeting_separately(site):
    """The whole point: a product under-pacing does not say which targeting."""
    app_module, order_id = site
    page = client_page(app_module, order_id, "strategy")

    assert "FB - Retargeting" in page
    assert "FB - Categories" in page
    # 20 days at 400 and 20 days at 3,800, matched onto the sold split.
    assert "8,000" in page
    assert "76,000" in page


def client_page(app_module, order_id, tab):
    return app_module.app.test_client().get(
        f"/orders/{order_id}?tab={tab}"
    ).get_data(as_text=True)


def test_a_strategy_finds_its_product_from_its_label(site):
    """"FB - Categories" is Meta's categories, not the order's.

    Only one of the two seeded rows carries a line item id; the other has to
    be placed by what its label names, matched against the products actually
    on this order.
    """
    import db as db_module
    import views

    app_module, order_id = site
    with db_module.session_scope() as session:
        view = views.order_view(session, order_id)
        blocks = views.strategy_blocks(session, view)

    meta = next(b for b in blocks if b.code == "M")
    assert sorted(r.label for r in meta.rows) == [
        "FB - Categories", "FB - Retargeting"
    ]
    # The PPC line has no strategies, and must not inherit Meta's.
    ppc = next(b for b in blocks if b.code == "PPC")
    assert ppc.rows == []


def test_a_buyer_can_add_targeting_no_export_mentions(site):
    from models import StrategyTerms

    import db as db_module

    app_module, order_id = site
    client = app_module.app.test_client()

    with db_module.session_scope() as session:
        from models import LineItem

        meta_id = (
            session.query(LineItem).filter(LineItem.external_id == "88001").one().id
        )

    client.post(
        f"/orders/{order_id}/strategies",
        data={"line_item_id": str(meta_id), "label": "Lookalike"},
        follow_redirects=True,
    )

    with db_module.session_scope() as session:
        added = (
            session.query(StrategyTerms)
            .filter(StrategyTerms.added_by_hand.is_(True))
            .one()
        )
        # Prefixed with the product's code, so it reads like the rest and
        # finds its product again on the next page load.
        assert added.label == "M - Lookalike"
        assert added.line_item_id == meta_id


def test_a_strategy_split_can_be_edited_and_removed(site):
    from models import StrategyTerms

    import db as db_module

    app_module, order_id = site
    client = app_module.app.test_client()

    with db_module.session_scope() as session:
        term = (
            session.query(StrategyTerms)
            .filter(StrategyTerms.label == "FB - Retargeting")
            .one()
        )
        term_id = term.id

    client.post(
        f"/orders/{order_id}/strategies/save",
        data={
            f"st-{term_id}-label": "FB - Retargeting",
            f"st-{term_id}-monthly": "12,500",
            f"st-{term_id}-total": "150000",
            f"st-{term_id}-rate": "5.10",
        },
        follow_redirects=True,
    )
    with db_module.session_scope() as session:
        term = session.get(StrategyTerms, term_id)
        assert (term.monthly_target, term.total_target, term.rate) == (
            12_500, 150_000, 5.10
        )

    client.post(
        f"/orders/{order_id}/strategies/{term_id}/delete", follow_redirects=True
    )
    with db_module.session_scope() as session:
        assert session.get(StrategyTerms, term_id) is None


def test_a_product_sold_in_spend_paces_on_spend(site):
    """Products on one order are not all sold the same way.

    A PPC line on a Display order paces against ad spend. Pacing it in
    impressions - which is how the order paces - answers nothing, and adding
    its dollars to the order's impressions would make a number that means
    nothing and looks like it means something.
    """
    import db as db_module
    import views

    app_module, order_id = site
    with db_module.session_scope() as session:
        view = views.order_view(session, order_id)

    kinds = {g.pacing_type: g for g in view.groups}
    assert set(kinds) == {"impression", "click"}
    assert kinds["click"].total.total_target == 18_000
    assert kinds["impression"].total.total_target == 1_536_000
    # The order's own total covers its own kind and says what it left out.
    assert view.total.total_target == 1_536_000
    assert view.total.mixed_types == ["click"]


# --- the buying team abbreviates ------------------------------------------
def test_the_targeting_matcher_reads_the_teams_own_shorthand():
    """A third of their sheet rows are abbreviations, and matched nothing.

    "SM - KW", "MC - GF", "FB - R" read by substring against full names like
    "keyword" hit nothing, so a third of every strategy split went unpaired -
    sold rows showing no delivery beside delivery claimed by nothing.

    A single letter cannot be searched for inside a whole label without
    landing in the middle of a word, so the product comes off the front first
    and the shorthand is matched against what is left.
    """
    from sheets import match_key

    assert match_key("SM - KW") == "keyword"
    assert match_key("MC - GF") == "geo-fencing"
    assert match_key("SM - GR") == "geo-retargeting"
    assert match_key("FB - R") == "retargeting"
    assert match_key("SM - B") == "behavioral"
    assert match_key("MC Categories") == "category"
    assert match_key("FB/IG - Categories") == "category"
    assert match_key("SM - CP") == "cross platform"
    assert match_key("D - Beh") == "behavioral"
    # "SM - B2B B" names an audience and a targeting. B2B is the more
    # distinguishing of the two and stays its own key: pairing it with plain
    # behavioral would quietly merge two rows the sheet keeps apart, and an
    # unpaired row is visible where a wrongly paired one is not.
    assert match_key("SM - B2B B") == "b2b"
    assert match_key("SM - B") == "behavioral"


def test_the_full_names_still_win_over_the_shorthand():
    """Longest first, so geo-retargeting is not read as retargeting."""
    from sheets import match_key

    assert match_key("FB - Retargeting") == "retargeting"
    assert match_key("MC - Geo-Retargeting") == "geo-retargeting"
    assert match_key("SM - Behavioral") == "behavioral"
    assert match_key("CTV - Geo-Fencing") == "geo-fencing"


def test_a_product_with_no_targeting_matches_nothing():
    """"Performance Max" names a product, not a way of targeting.

    Guessing one would pair a sold row with delivery it has nothing to do
    with, which is worse than leaving it unpaired and visible.
    """
    from sheets import match_key

    assert match_key("Performance Max") is None
    assert match_key("PPC") is None
    assert match_key("Video") is None
    assert match_key("") is None
