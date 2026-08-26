from backend.main import _credentials


def test_demo_control_totals(client):
    response = client.get("/api/dashboard?day=2026-07-02")
    assert response.status_code == 200
    kpi = response.json()["kpi"]
    assert kpi["sold"] == 678
    assert kpi["revenue"] == 627729.15
    assert kpi["ads"] == 81876
    assert kpi["extra"] == 23010
    assert kpi["profit"] == 22740.55
    assert kpi["drr"] == 13.04
    assert kpi["margin"] == 3.62
    assert response.json()["leaders"][0]["sku"] == "а3"


def test_demo_has_exactly_34_products_and_68_daily_rows(client):
    assert client.get("/api/products").json()["summary"]["active"] == 34
    assert client.get("/api/daily?day=2026-07-01").json()["totals"]["sku_count"] == 34
    assert client.get("/api/daily?day=2026-07-02").json()["totals"]["sku_count"] == 34
    assert client.get("/api/daily?day=2026-07-03").json()["totals"]["sku_count"] == 0


def test_manual_cost_recalculates_profit_not_drr(client):
    before = client.get("/api/dashboard").json()["kpi"]
    created = client.post("/api/costs", json={"day": "2026-07-02", "kind": "extra", "amount": 1000, "comment": "Тест"})
    assert created.status_code == 201
    after = client.get("/api/dashboard").json()["kpi"]
    assert after["profit"] == before["profit"] - 1000
    assert after["extra"] == before["extra"] + 1000
    assert after["drr"] == before["drr"]


def test_api_key_is_never_returned(client):
    _credentials.update(client_id="123", api_key="top-secret")
    payload = client.get("/api/ozon/status").json()
    assert payload["api_key"] is None
    assert "top-secret" not in client.get("/api/ozon/status").text
    _credentials.clear()


def test_export_matches_current_day(client):
    response = client.get("/api/export.xlsx?day=2026-07-02")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"
    assert "spreadsheetml" in response.headers["content-type"]
