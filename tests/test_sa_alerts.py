"""StackAdapt alerts posted by the launcher's nightly run show on the Pacing tab."""
from __future__ import annotations

from tests.test_details import site  # noqa: F401  (fixture)

ALERT = {
    "kind": "high_cpm_overshare", "campaign_id": "3454985", "campaign": "Forrest - Display - Behavioral",
    "alert": "72% of imps vs 49% planned at $2.00 eCPM (group $1.72)", "adjustment": "bid $2.00 -> $1.80",
    "order_id": "51200", "line_item_id": "99001", "group_id": "G1", "group": "Forrest - Display Ads - 99001",
    "status": "dry run",
}


def test_alerts_need_the_token(site, monkeypatch):  # noqa: F811
    app_module, _ = site
    client = app_module.app.test_client()
    monkeypatch.delenv("SA_ALERTS_TOKEN", raising=False)
    assert client.post("/api/sa-alerts", json={"run_date": "2026-11-05", "alerts": []}).status_code == 403
    monkeypatch.setenv("SA_ALERTS_TOKEN", "t")
    r = client.post("/api/sa-alerts", json={"run_date": "2026-11-05", "alerts": []},
                    headers={"X-Alerts-Token": "nope"})
    assert r.status_code == 403


def test_newest_run_shows_on_order_and_product(site, monkeypatch):  # noqa: F811
    app_module, (order_id, li_id) = site
    client = app_module.app.test_client()
    monkeypatch.setenv("SA_ALERTS_TOKEN", "t")
    h = {"X-Alerts-Token": "t"}
    low = {**ALERT, "kind": "low_ecpm", "campaign": "", "campaign_id": "",
           "alert": "eCPM $0.88 vs $2.13 planned (-59%)", "adjustment": "budget $200.00 -> $113.53"}
    assert client.post("/api/sa-alerts", json={"run_date": "2026-11-04", "alerts": [low]}, headers=h).status_code == 200
    r = client.post("/api/sa-alerts", json={"run_date": "2026-11-05", "alerts": [ALERT, ALERT]}, headers=h)
    assert r.get_json()["alerts"] == 2
    # Re-posting a run date replaces it.
    client.post("/api/sa-alerts", json={"run_date": "2026-11-05", "alerts": [ALERT]}, headers=h)

    order = client.get(f"/orders/{order_id}").get_data(as_text=True)
    assert "StackAdapt Alerts" in order and "High CPM over share" in order
    assert order.count("bid $2.00 -&gt; $1.80") == 1
    assert "Low eCPM" not in order  # older run
    product = client.get(f"/orders/{order_id}/details/{li_id}").get_data(as_text=True)
    assert "High CPM over share" in product and "Dry run" in product


def test_no_alerts_no_card(site):  # noqa: F811
    app_module, (order_id, _) = site
    html = app_module.app.test_client().get(f"/orders/{order_id}").get_data(as_text=True)
    assert "StackAdapt Alerts" not in html
