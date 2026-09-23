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


# --- what is running, and drafting a split from it -------------------------
def test_the_running_targeting_and_its_share_are_shown(site):
    """What is running needs nobody to type it; the sold split does.

    A product with no split entered used to show an empty card - true, and
    useless. The shares actually running are the obvious first draft of the
    split, and seeing them is most of the work.
    """
    import db as db_module
    import views

    app_module, order_id = site
    with db_module.session_scope() as session:
        view = views.order_view(session, order_id)
        blocks = views.strategy_blocks(session, view)

    meta = next(b for b in blocks if b.code == "M")
    shares = {o.label: round(o.share, 3) for o in meta.observed}
    # 20 days at 3,800 against 20 days at 400.
    assert shares == {
        "META - Categories": 0.905, "META - Retargeting": 0.095
    }
    assert meta.observed_total == 84_000
    assert all(o.claimed for o in meta.observed), "both have a sold row"


def test_a_split_can_be_drafted_from_what_is_running(site):
    """Apportion the product's sold figures by the shares in the feed."""
    from models import LineItem, StrategyTerms

    import db as db_module

    app_module, order_id = site
    client = app_module.app.test_client()

    # Clear the seeded split so there is a gap to fill.
    with db_module.session_scope() as session:
        for term in session.query(StrategyTerms).all():
            session.delete(term)
        meta_id = (
            session.query(LineItem).filter(LineItem.external_id == "88001").one().id
        )

    client.post(
        f"/orders/{order_id}/strategies/seed",
        data={"line_item_id": str(meta_id)},
        follow_redirects=True,
    )

    with db_module.session_scope() as session:
        drafted = {
            t.label: (t.monthly_target, t.total_target)
            for t in session.query(StrategyTerms).all()
        }

    # The product sold 128,000 a month and 1,536,000 in total.
    assert len(drafted) == 2
    monthly = sum(v[0] for v in drafted.values())
    total = sum(v[1] for v in drafted.values())
    assert round(monthly) == 128_000
    assert round(total) == 1_536_000


def test_drafting_never_overwrites_a_row_a_buyer_entered(site):
    """It fills gaps. Someone's typed numbers are not a gap."""
    from models import LineItem, StrategyTerms

    import db as db_module

    app_module, order_id = site
    client = app_module.app.test_client()

    with db_module.session_scope() as session:
        meta_id = (
            session.query(LineItem).filter(LineItem.external_id == "88001").one().id
        )
        before = {
            t.label: t.monthly_target for t in session.query(StrategyTerms).all()
        }

    client.post(
        f"/orders/{order_id}/strategies/seed",
        data={"line_item_id": str(meta_id)},
        follow_redirects=True,
    )

    with db_module.session_scope() as session:
        after = {
            t.label: t.monthly_target for t in session.query(StrategyTerms).all()
        }
    # The seeded rows are named differently from the feed's, so drafting adds
    # the feed's - but it must not have touched what was already there.
    for label, monthly in before.items():
        assert after[label] == monthly


def test_each_product_is_charted_in_its_own_unit(site):
    """Impressions for a product sold in impressions, spend for one in spend.

    They cannot share an axis: a Display line running 40,000 a day beside a
    PPC line spending $90 would flatten one against the other and say
    nothing about either.
    """
    import db as db_module
    import views

    app_module, order_id = site
    with db_module.session_scope() as session:
        charts = views.product_charts(views.order_view(session, order_id))

    units = {c["label"]: c["unit"] for c in charts}
    assert units["Impressions"] == "Impressions per day"
    assert [s["label"] for s in charts[0]["series"]] == ["Meta"]


def test_a_spend_product_reports_its_strategies_in_spend(site):
    """Asking a Pay-Per-Click line for impressions reports nothing ran.

    Its strategies were measured in the order's unit rather than their own,
    so every one of them read as zero on a product that was spending fine.
    """
    import datetime as dt

    from models import DailyDelivery

    import db as db_module
    import views

    app_module, order_id = site
    with db_module.session_scope() as session:
        for day in range(1, 11):
            session.add(
                DailyDelivery(
                    date=dt.date(2026, 8, day),
                    data_source="Google Ads Search",
                    campaign_id="PPC-1",
                    strategy_id=f"SRCH-{day}",
                    client_name="Bud's Auto",
                    external_order_id="44100",
                    external_line_item_id="88002",
                    product="Pay-Per-Click Ads",
                    strategy_name="Search",
                    impressions=0.0,
                    clicks=30.0,
                    cost=55.0,
                )
            )

    with db_module.session_scope() as session:
        view = views.order_view(session, order_id)
        blocks = views.strategy_blocks(session, view)

    ppc = next(b for b in blocks if b.code == "PPC")
    assert ppc.observed_total == 550.0, "10 days at $55, not zero impressions"


# --- the tab bar and the route have to agree -------------------------------
def test_every_tab_link_on_the_page_selects_its_own_pane(site):
    """Renaming Campaign to Product broke this and nothing caught it.

    The template linked to `tab=product`; the route still only accepted
    `tab=campaign`, so the new link fell through to the default and clicking
    Product showed the Order pane. Both are now built from one list, and this
    follows every link the page actually renders rather than a list written
    out again here.
    """
    import re

    app_module, order_id = site
    client = app_module.app.test_client()
    page = client.get(f"/orders/{order_id}").get_data(as_text=True)

    links = re.findall(r'href="([^"]*\btab=[a-z]+)"', page)
    assert len(links) >= 3, "the tab bar should be on the page"

    for href in links:
        name = href.rsplit("tab=", 1)[1]
        body = client.get(href.replace("&amp;", "&")).get_data(as_text=True)
        panes = re.findall(r'<div class="pane"[^>]*>', body)
        shown = [i for i, pane in enumerate(panes) if "hidden" not in pane]
        assert len(shown) == 1, f"{name}: {len(shown)} panes visible"
        # And the tab itself reads as the selected one.
        assert re.search(
            r'class="tab on"[^>]*href="[^"]*tab=' + name, body
        ), f"{name} is not marked selected"


def test_the_old_campaign_link_still_lands_on_product(site):
    """Links to the old name are out there."""
    import re

    app_module, order_id = site
    body = app_module.app.test_client().get(
        f"/orders/{order_id}?tab=campaign"
    ).get_data(as_text=True)
    assert re.search(r'class="tab on"[^>]*href="[^"]*tab=product', body)


# --- drafting across the whole book ----------------------------------------
def test_drafting_fills_every_live_order_that_has_no_split(site):
    """Doing this by hand across a thousand orders is not work that finishes."""
    from models import StrategyTerms

    import db as db_module
    import views

    app_module, order_id = site
    with db_module.session_scope() as session:
        for term in session.query(StrategyTerms).all():
            session.delete(term)

    with db_module.session_scope() as session:
        result = views.draft_missing_splits(session)

    assert result.orders_seen == 1
    assert result.orders_drafted == 1
    assert result.strategies_added == 2

    with db_module.session_scope() as session:
        drafted = session.query(StrategyTerms).all()
        assert {t.source for t in drafted} == {"drafted from delivery"}
        assert all(t.line_item_id is not None for t in drafted)


def test_drafting_skips_an_order_that_already_has_a_split(site):
    """It fills gaps. A split already entered is not a gap."""
    import db as db_module
    import views

    app_module, order_id = site
    with db_module.session_scope() as session:
        result = views.draft_missing_splits(session)

    assert result.orders_seen == 0
    assert result.strategies_added == 0


def test_drafting_is_safe_to_run_twice(site):
    """Someone will press it twice. The second press must change nothing."""
    from models import StrategyTerms

    import db as db_module
    import views

    app_module, order_id = site
    with db_module.session_scope() as session:
        for term in session.query(StrategyTerms).all():
            session.delete(term)

    with db_module.session_scope() as session:
        views.draft_missing_splits(session)
    with db_module.session_scope() as session:
        again = views.draft_missing_splits(session)

    assert again.strategies_added == 0
    with db_module.session_scope() as session:
        assert session.query(StrategyTerms).count() == 2


def test_two_products_on_one_order_can_run_the_same_targeting(site):
    """Two Mobile Conquesting lines both running behavioral is two rows.

    The label was unique per order, so the second one could not be written
    at all - drafting across the book died on a unique violation partway
    through. A strategy belongs to a product, so that is the grain.
    """
    import datetime as dt

    from models import DailyDelivery, LineItem, Order, StrategyTerms

    import db as db_module
    import views

    app_module, order_id = site
    with db_module.session_scope() as session:
        for term in session.query(StrategyTerms).all():
            session.delete(term)
        order = session.get(Order, order_id)
        # A second line item of the same product as the first.
        twin = LineItem(
            order_id=order.id, external_id="88003", name="Meta 2",
            product="Meta Display & Video Ads", sort_order=2,
            monthly_impressions=40_000.0, total_impressions=480_000.0,
            goal_cpm=2.96,
        )
        session.add(twin)
        session.flush()
        for day in range(1, 11):
            session.add(
                DailyDelivery(
                    date=dt.date(2026, 8, day),
                    data_source="Meta",
                    campaign_id="CMP-2",
                    strategy_id=f"TWIN-{day}",
                    client_name="Bud's Auto",
                    external_order_id="44100",
                    external_line_item_id="88003",
                    product="Meta Display & Video Ads",
                    strategy_name="Meta - Categories",
                    strategy_type="Categories",
                    impressions=900.0,
                )
            )

    with db_module.session_scope() as session:
        views.draft_missing_splits(session)

    with db_module.session_scope() as session:
        rows = session.query(StrategyTerms).all()
        by_label = {}
        for row in rows:
            by_label.setdefault(row.label, []).append(row.line_item_id)

    # The same targeting, on two products, is two rows on one order.
    assert len(by_label["META - Categories"]) == 2
    assert len(set(by_label["META - Categories"])) == 2
