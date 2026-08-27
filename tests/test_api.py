import io
from datetime import datetime, timezone

from openpyxl import load_workbook

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


def test_manual_ad_cost_updates_dashboard_daily_and_export(client):
    before = client.get("/api/dashboard?day=2026-07-02").json()["kpi"]
    created = client.post(
        "/api/costs",
        json={"day": "2026-07-02", "kind": "ads", "amount": 1000, "comment": "Реклама"},
    )
    assert created.status_code == 201

    dashboard = client.get("/api/dashboard?day=2026-07-02").json()["kpi"]
    daily = client.get("/api/daily?day=2026-07-02").json()
    assert dashboard["ads"] == before["ads"] + 1000
    assert dashboard["extra"] == before["extra"]
    assert dashboard["profit"] == before["profit"] - 1000
    assert dashboard["drr"] > before["drr"]
    assert daily["totals"]["ads"] == dashboard["ads"]
    assert daily["totals"]["profit"] == dashboard["profit"]
    assert daily["unallocated_costs"]["ads"] == 1000

    workbook = load_workbook(io.BytesIO(client.get("/api/export.xlsx?day=2026-07-02").content))
    values = list(workbook.active.values)
    assert any(row[0] == "Общий расход дня" and row[5] == 1000 for row in values)
    assert values[-1][0] == "Итого"
    assert values[-1][5] == dashboard["ads"]


def test_product_cost_is_applied_to_its_daily_row(client):
    before = client.get("/api/daily?day=2026-07-02").json()["items"][0]
    response = client.post(
        "/api/costs",
        json={"day": "2026-07-02", "kind": "extra", "amount": 250,
              "product_id": before["product_id"], "comment": "Фото"},
    )
    assert response.status_code == 201
    after = next(
        item for item in client.get("/api/daily?day=2026-07-02").json()["items"]
        if item["product_id"] == before["product_id"]
    )
    assert after["extra_costs"] == before["extra_costs"] + 250
    assert after["profit"] == before["profit"] - 250


def test_manual_cost_can_be_updated_and_deleted(client):
    created = client.post(
        "/api/costs",
        json={"day": "2026-07-02", "kind": "extra", "amount": 100, "comment": "Черновик"},
    ).json()
    updated = client.put(
        f'/api/costs/{created["id"]}',
        json={"day": "2026-07-02", "kind": "ads", "amount": 300, "comment": "Исправлено"},
    )
    assert updated.status_code == 200
    item = client.get("/api/costs?day=2026-07-02").json()[0]
    assert item["kind"] == "ads"
    assert item["amount"] == 300
    assert client.delete(f'/api/costs/{created["id"]}').status_code == 204
    assert client.get("/api/costs?day=2026-07-02").json() == []


def test_unit_economics_can_be_versioned(client):
    response = client.put(
        "/api/products/7/economics",
        json={"purchase_price": 250, "marking": 6, "packaging": 20,
              "inbound_delivery": 13, "cross_dock": 8, "tax_rate": 0.06,
              "planned_commission": 0.2, "planned_logistics": 100,
              "other_fixed": 5, "valid_from": "2026-08-01"},
    )
    assert response.status_code == 200
    product = client.get("/api/products?search=0%205-1").json()["items"][0]
    assert product["purchase_price"] == 250
    assert product["packaging"] == 20


def test_manual_product_can_be_created_and_edited(client):
    created = client.post(
        "/api/products",
        json={"name": "Новый товар", "sku_original": "manual-01", "price": 1290, "scheme": "FBS"},
    )
    assert created.status_code == 201, created.text
    product_id = created.json()["id"]
    assert created.json()["source"] == "manual"
    assert created.json()["mapping_status"] == "unmatched"

    updated = client.put(
        f"/api/products/{product_id}",
        json={"name": "Обновлённый товар", "sku_original": "manual-02", "price": 1390, "scheme": "FBO"},
    )
    assert updated.status_code == 200
    item = client.get("/api/products?search=manual-02").json()["items"][0]
    assert item["name"] == "Обновлённый товар"
    assert item["price"] == 1390
    assert item["scheme"] == "FBO"

    duplicate = client.post(
        "/api/products",
        json={"name": "Дубль", "sku_original": "01", "price": 0, "scheme": None},
    )
    assert duplicate.status_code == 409


def test_daily_note_can_be_saved_and_removed(client):
    product = client.get("/api/daily?day=2026-07-02").json()["items"][0]
    saved = client.put(
        f'/api/daily/2026-07-02/{product["product_id"]}/note',
        json={"comment": "Проверить рекламную кампанию"},
    )
    assert saved.status_code == 200
    updated = next(
        item for item in client.get("/api/daily?day=2026-07-02").json()["items"]
        if item["product_id"] == product["product_id"]
    )
    assert updated["note"] == "Проверить рекламную кампанию"
    client.put(f'/api/daily/2026-07-02/{product["product_id"]}/note', json={"comment": ""})
    cleared = next(
        item for item in client.get("/api/daily?day=2026-07-02").json()["items"]
        if item["product_id"] == product["product_id"]
    )
    assert cleared["note"] == ""


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


def test_sync_inserts_unknown_products_and_builds_live_kpi(client, monkeypatch):
    occurred_at = datetime.now(timezone.utc).isoformat()

    class FakeOzonClient:
        def __init__(self, **_):
            pass

        def products(self):
            return {"result": {"items": [{"offer_id": "live-1", "product_id": 9001}]}}

        def fbs_postings(self, *_):
            return {"result": {"postings": [{
                "posting_number": "fbs-live-1", "status": "delivered",
                "in_process_at": occurred_at,
                "products": [{"offer_id": "live-1", "product_id": 9001,
                              "name": "Живой товар", "price": "1000", "quantity": 2}],
                "financial_data": {"products": [{"offer_id": "live-1",
                                                   "commission_amount": -100,
                                                   "item_services": {}}],
                                   "posting_services": {}},
            }]}}

        def fbo_postings(self, *_):
            return {"result": []}

        def finance(self, *_):
            return {"result": {"operations": [{"operation_id": "op-live-1",
                                                  "operation_date": occurred_at,
                                                  "operation_type": "OperationAgentDeliveredToCustomer",
                                                  "amount": 1900}]}}

    monkeypatch.setattr("backend.main.OzonClient", FakeOzonClient)
    _credentials.update(client_id="123", api_key="secret")
    response = client.post("/api/ozon/sync")
    _credentials.clear()
    assert response.status_code == 200, response.text
    day = response.json()["latest_day"]
    report = client.get(f"/api/daily?day={day}&search=live-1").json()
    assert len(report["items"]) == 1
    assert report["items"][0]["name"] == "Живой товар"
    assert report["items"][0]["source"] == "ozon"
    assert report["items"][0]["sold"] == 2
    assert report["items"][0]["revenue"] == 2000
    assert client.get("/api/products?search=live-1").json()["summary"]["active"] == 1

    ozon_product = client.get("/api/ozon/catalog-products").json()[0]
    local = client.post(
        "/api/products",
        json={"name": "Локальная карточка", "sku_original": "local-live-1",
              "price": 0, "scheme": "FBS"},
    ).json()
    binding = client.put(
        f'/api/products/{local["id"]}/binding',
        json={"target_product_id": ozon_product["id"]},
    )
    assert binding.status_code == 200, binding.text
    rebound = client.get(f"/api/daily?day={day}&search=local-live-1").json()["items"]
    assert len(rebound) == 1
    assert rebound[0]["product_id"] == local["id"]
    assert rebound[0]["sold"] == 2
    bound_product = client.get("/api/products?search=local-live-1").json()["items"][0]
    assert bound_product["name"] == "Локальная карточка"
    assert bound_product["offer_id"] == "live-1"
    assert bound_product["ozon_product_id"] == 9001

    assert client.delete(f'/api/products/{local["id"]}/binding').status_code == 204
    unbound = client.get("/api/products?search=local-live-1").json()["items"][0]
    assert unbound["mapping_status"] == "unmatched"
    assert unbound["ozon_product_id"] is None
