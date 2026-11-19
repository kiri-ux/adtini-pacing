"""Scripts read the RZ-synced order book from /api/lines with a token."""
from __future__ import annotations

from tests.test_details import site  # noqa: F401  (fixture)


def test_lines_need_the_token(site, monkeypatch):  # noqa: F811
    app_module, _ = site
    client = app_module.app.test_client()
    monkeypatch.delenv("EXPORT_TOKEN", raising=False)
    assert client.get("/api/lines").status_code == 403
    monkeypatch.setenv("EXPORT_TOKEN", "t")
    assert client.get("/api/lines", headers={"X-Export-Token": "nope"}).status_code == 403


def test_lines_list_the_book(site, monkeypatch):  # noqa: F811
    app_module, _ = site
    client = app_module.app.test_client()
    monkeypatch.setenv("EXPORT_TOKEN", "t")
    r = client.get("/api/lines", headers={"X-Export-Token": "t"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == len(body["lines"]) >= 1
    line = body["lines"][0]
    assert line["order_id"] == "51200" and line["client"] == "Forrest Design Group"
    assert line["order_status"] == "IO Live" and line["end_date"]
