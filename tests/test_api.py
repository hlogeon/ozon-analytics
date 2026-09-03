import io
import time
from datetime import date, datetime, timedelta, timezone

from openpyxl import load_workbook

from backend.database import connection, json_dump
from backend.main import _credentials
from backend.ozon import OzonError
from backend.sync import (
    OVERLAP_DAYS,
    MAX_WINDOW_DAYS,
    DEFAULT_WINDOW_DAYS,
    _sync_lock,
    begin_sync_run,
    execute_sync_run,
    iter_day_chunks,
    rebuild_live_kpi,
    sync_window,
)
from tests.factories import make_kpi, make_product, make_sync_run, seed_sample


def _wait_until_not_running(client, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/api/ozon/status").json()
        if not status["running"]:
            return status
        time.sleep(0.02)
    raise AssertionError("Синхронизация не завершилась вовремя")


def test_empty_dashboard_returns_payload_not_error(client):
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    payload = response.json()
    assert payload["empty"] is True
    assert payload["kpi"] is None
    assert payload["leaders"] == []


def test_sample_dashboard_totals(client):
    seed_sample()
    response = client.get("/api/dashboard?day=2026-07-02")
    assert response.status_code == 200
    kpi = response.json()["kpi"]
    assert kpi["sold"] == 23
    assert kpi["revenue"] == 21400
    assert kpi["ads"] == 3100
    assert kpi["extra"] == 230
    assert kpi["profit"] == 2600
    assert response.json()["leaders"][0]["sku"] == "а3"
    assert response.json()["empty"] is False


def test_sample_has_two_days_of_kpi(client):
    seed_sample()
    assert client.get("/api/products").json()["summary"]["active"] == 2
    assert client.get("/api/daily?day=2026-07-01").json()["totals"]["sku_count"] == 2
    assert client.get("/api/daily?day=2026-07-02").json()["totals"]["sku_count"] == 2
    assert client.get("/api/daily?day=2026-07-03").json()["totals"]["sku_count"] == 0


def test_manual_cost_recalculates_profit_not_drr(client):
    seed_sample()
    before = client.get("/api/dashboard?day=2026-07-02").json()["kpi"]
    created = client.post("/api/costs", json={"day": "2026-07-02", "kind": "extra", "amount": 1000, "comment": "Тест"})
    assert created.status_code == 201
    after = client.get("/api/dashboard?day=2026-07-02").json()["kpi"]
    assert after["profit"] == before["profit"] - 1000
    assert after["extra"] == before["extra"] + 1000
    assert after["drr"] == before["drr"]


def test_manual_ad_cost_updates_dashboard_daily_and_export(client):
    seed_sample()
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
    seed_sample()
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
    seed_sample()
    product = client.get("/api/products?search=0%205-1").json()["items"][0]
    response = client.put(
        f"/api/products/{product['id']}/economics",
        json={"purchase_price": 250, "marking": 6, "packaging": 20,
              "inbound_delivery": 13, "cross_dock": 8, "tax_rate": 0.06,
              "planned_commission": 0.2, "planned_logistics": 100,
              "other_fixed": 5, "valid_from": "2026-10-01"},
    )
    assert response.status_code == 200
    updated = client.get("/api/products?search=0%205-1").json()["items"][0]
    assert updated["purchase_price"] == 250
    assert updated["packaging"] == 20


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
        json={"name": "Дубль", "sku_original": "manual-02", "price": 0, "scheme": None},
    )
    assert duplicate.status_code == 409


def test_daily_note_can_be_saved_and_removed(client):
    seed_sample()
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
    assert payload["running"] is False
    assert payload["interval_minutes"] == 0
    _credentials.clear()


def test_export_matches_current_day(client):
    seed_sample()
    response = client.get("/api/export.xlsx?day=2026-07-02")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"
    assert "spreadsheetml" in response.headers["content-type"]


def test_timeseries_matches_daily_totals(client):
    seed_sample()
    series = client.get("/api/timeseries?from=2026-07-01&to=2026-07-02").json()
    day_one = client.get("/api/daily?day=2026-07-01").json()["totals"]
    day_two = client.get("/api/daily?day=2026-07-02").json()["totals"]
    assert series["items"][0]["revenue"] == day_one["revenue"]
    assert series["items"][0]["profit"] == day_one["profit"]
    assert series["items"][1]["revenue"] == day_two["revenue"]
    assert series["items"][1]["profit"] == day_two["profit"]
    assert series["items"][0]["sold"] == day_one["sold"]
    assert series["max_value"] == max(day_one["revenue"], day_two["revenue"], abs(day_one["profit"]), abs(day_two["profit"]))


def test_alerts_cover_each_signal(client):
    first = make_product(name="Убыточный", sku="loss-1", mapping_status="unmatched", purchase_price=0)
    second = make_product(name="Дорогой трафик", sku="ads-1", mapping_status="linked",
                          purchase_price=100, offer_id="ads-1", source="ozon")
    make_product(name="Без продаж и себестоимости", sku="idle-1", purchase_price=0)
    make_kpi(first, "2026-07-02", sold=3, revenue=1000, ads=0, profit=-250)
    make_kpi(second, "2026-07-02", sold=5, revenue=2000, ads=600, profit=100)
    make_sync_run(status="error", error="Ozon API временно недоступен")
    payload = client.get("/api/alerts?from=2026-07-02&to=2026-07-02").json()
    codes = {item["code"] for item in payload["items"]}
    assert "loss_makers" in codes
    assert "high_drr" in codes
    assert "missing_cost" in codes
    assert "unlinked" in codes
    assert "sync_failed" in codes
    missing = next(item for item in payload["items"] if item["code"] == "missing_cost")
    assert missing["count"] == 2
    assert missing["count"] == client.get("/api/products").json()["summary"]["without_cost"]
    assert payload["total"] == len(payload["items"])


def test_alerts_flag_stale_when_no_successful_sync_ever_happened(client):
    payload = client.get("/api/alerts?from=2026-07-02&to=2026-07-02").json()
    codes = {item["code"] for item in payload["items"]}
    assert "sync_stale" in codes


def test_alerts_flag_stale_after_threshold_hours(client):
    old = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    make_sync_run(status="success", finished_at=old)
    payload = client.get("/api/alerts?from=2026-07-02&to=2026-07-02").json()
    codes = {item["code"] for item in payload["items"]}
    assert "sync_stale" in codes


def test_alerts_no_stale_signal_right_after_success(client):
    recent = datetime.now(timezone.utc).isoformat()
    make_sync_run(status="success", finished_at=recent)
    payload = client.get("/api/alerts?from=2026-07-02&to=2026-07-02").json()
    codes = {item["code"] for item in payload["items"]}
    assert "sync_stale" not in codes


def test_sync_window_defaults_to_14_days():
    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    since, until = sync_window(now)
    assert since == now - timedelta(days=DEFAULT_WINDOW_DAYS)
    assert until == now


def test_sync_window_uses_overlap_from_last_success():
    make_sync_run(
        status="success",
        window_from="2026-08-01T00:00:00+00:00",
        window_to="2026-08-20T12:00:00+00:00",
    )
    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    since, until = sync_window(now)
    assert since == datetime(2026, 8, 20, 12, tzinfo=timezone.utc) - timedelta(days=OVERLAP_DAYS)
    assert until == now


def test_sync_window_caps_at_max_days():
    make_sync_run(status="success", window_to="2025-01-01T00:00:00+00:00")
    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    since, _until = sync_window(now)
    assert since == now - timedelta(days=MAX_WINDOW_DAYS)


def test_sync_window_resumes_from_partial_runs_too():
    make_sync_run(status="partial", window_to="2026-08-20T12:00:00+00:00")
    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    since, until = sync_window(now)
    assert since == datetime(2026, 8, 20, 12, tzinfo=timezone.utc) - timedelta(days=OVERLAP_DAYS)
    assert until == now


def test_sync_window_ignores_fast_and_manual_runs():
    make_sync_run(task="fast", status="success", window_to="2026-08-22T12:00:00+00:00")
    make_sync_run(task="manual-range", status="success", window_to="2026-08-21T21:00:00+00:00")
    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    since, until = sync_window(now)
    assert since == now - timedelta(days=DEFAULT_WINDOW_DAYS)
    assert until == now


def test_sync_window_keeps_full_watermark_after_a_fast_run():
    make_sync_run(task="full", status="success", window_to="2026-08-20T12:00:00+00:00")
    make_sync_run(task="fast", status="success", window_to="2026-08-22T12:00:00+00:00")
    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    since, _until = sync_window(now)
    assert since == datetime(2026, 8, 20, 12, tzinfo=timezone.utc) - timedelta(days=OVERLAP_DAYS)


def test_iter_day_chunks_splits_window_into_moscow_calendar_days():
    window_from = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    window_to = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)
    chunks = list(iter_day_chunks(window_from, window_to))
    assert [chunk[0] for chunk in chunks] == [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
    assert chunks[0][1] == window_from
    assert chunks[-1][2] == window_to
    for _day, start, end in chunks:
        assert start < end


def test_iter_day_chunks_empty_for_inverted_window():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert list(iter_day_chunks(now, now)) == []


def test_rebuild_live_kpi_attributes_late_utc_posting_to_its_moscow_day(client):
    make_product(name="Полночный", sku="midnight-1", source="ozon",
                 mapping_status="linked", purchase_price=0, offer_id="midnight-1")
    posting = {
        "posting_number": "midnight-1",
        "status": "delivered",
        "products": [{"offer_id": "midnight-1", "price": "500", "quantity": 1}],
        "financial_data": {"products": [{"offer_id": "midnight-1", "commission_amount": -10}]},
    }
    occurred_at = "2026-08-20T22:00:00.000000Z"  # 2026-08-21 01:00 in Moscow
    with connection() as db:
        db.execute(
            "INSERT INTO ozon_postings(posting_number,scheme,status,occurred_at,raw_json) VALUES(?,?,?,?,?)",
            (posting["posting_number"], "FBS", posting["status"], occurred_at, json_dump(posting)),
        )
        rows_for_20 = rebuild_live_kpi(db, "2026-08-20")
        rows_for_21 = rebuild_live_kpi(db, "2026-08-21")
    assert rows_for_20 == 0
    assert rows_for_21 == 1
    assert client.get("/api/daily?day=2026-08-20").json()["totals"]["sku_count"] == 0
    assert client.get("/api/daily?day=2026-08-21").json()["totals"]["sku_count"] == 1


def test_execute_sync_run_keeps_completed_days_and_marks_partial(client, monkeypatch):
    window_from = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    window_to = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)
    calls = {"fbs": 0}

    class FlakyOzonClient:
        def __init__(self, **_):
            pass

        def iter_products(self, **_):
            return []

        def iter_product_info(self, *_args, **_kwargs):
            return []

        def iter_postings(self, scheme, since, *_args, **_kwargs):
            if scheme != "FBS":
                return
            calls["fbs"] += 1
            if calls["fbs"] == 2:
                raise OzonError("Ozon недоступен")
            yield {
                "posting_number": f"posting-{since}",
                "status": "delivered",
                "in_process_at": since,
                "products": [{"offer_id": "chunk-1", "price": "100", "quantity": 1}],
                "financial_data": {"products": [{"offer_id": "chunk-1", "commission_amount": -10}]},
            }

        def iter_finance(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr("backend.sync.OzonClient", FlakyOzonClient)
    run_id = begin_sync_run("test", window_from, window_to)
    result = execute_sync_run(run_id, {"client_id": "1", "api_key": "k"}, window_from, window_to)
    assert result["status"] == "partial"

    last_run = client.get("/api/sync-runs").json()[0]
    assert last_run["status"] == "partial"
    assert last_run["window_to"] < window_to.isoformat()

    kept = client.get("/api/daily?day=2026-09-01").json()
    assert kept["totals"]["sku_count"] == 1


def test_sync_rejects_when_already_running(client):
    _credentials.update(client_id="123", api_key="secret")
    assert _sync_lock.acquire(blocking=False)
    try:
        response = client.post("/api/ozon/sync")
        assert response.status_code == 409
    finally:
        _sync_lock.release()
        _credentials.clear()


def test_sync_inserts_unknown_products_and_builds_live_kpi(client, monkeypatch):
    occurred_at = datetime.now(timezone.utc).isoformat()

    class FakeOzonClient:
        def __init__(self, **_):
            pass

        def iter_products(self, **_):
            yield {"offer_id": "live-1", "product_id": 9001}

        def iter_product_info(self, *_args, **_kwargs):
            return []

        def iter_postings(self, scheme, *_args, **_kwargs):
            if scheme != "FBS":
                return
            yield {
                "posting_number": "fbs-live-1", "status": "delivered",
                "in_process_at": occurred_at,
                "products": [{"offer_id": "live-1", "product_id": 9001,
                              "name": "Живой товар", "price": "1000", "quantity": 2}],
                "financial_data": {"products": [{"offer_id": "live-1",
                                                   "commission_amount": -100,
                                                   "item_services": {}}],
                                   "posting_services": {}},
            }

        def iter_finance(self, *_args, **_kwargs):
            yield {"operation_id": "op-live-1", "operation_date": occurred_at,
                   "operation_type": "OperationAgentDeliveredToCustomer", "amount": 1900}

    monkeypatch.setattr("backend.sync.OzonClient", FakeOzonClient)
    _credentials.update(client_id="123", api_key="secret")
    response = client.post("/api/ozon/sync")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "started"
    status = _wait_until_not_running(client)
    _credentials.clear()
    assert status["last_run"]["status"] == "success", status["last_run"]
    day = client.get("/api/dashboard").json()["latest_day"]
    report = client.get(f"/api/daily?day={day}&search=live-1").json()
    assert len(report["items"]) == 1
    assert report["items"][0]["name"] == "Живой товар"
    assert report["items"][0]["source"] == "ozon"
    assert report["items"][0]["sold"] == 2
    assert report["items"][0]["revenue"] == 2000
    assert client.get("/api/products?search=live-1").json()["summary"]["active"] == 1
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["source"] == "ozon"
    assert dashboard["day"] == day

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
