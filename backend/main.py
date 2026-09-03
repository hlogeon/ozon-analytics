from __future__ import annotations

import io
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .config import ozon_credentials
from .database import connection, init_db, one, rows
from .ozon import OzonClient, OzonError
from .reports import alerts_report, daily_report, latest_kpi_day, timeseries_report
from .skus import normalize_sku
from .sync import (
    MOSCOW,
    SyncBusyError,
    begin_sync_run,
    execute_sync_run,
    moscow_day_bounds,
    recover_stale_runs,
    scheduler_status,
    start_scheduler,
    stop_scheduler,
    sync_window,
)

FAST_SYNC_LOOKBACK_DAYS = 2

_credentials: dict[str, str] = {}
_scheduler_task = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _scheduler_task
    init_db()
    recover_stale_runs()
    _credentials.update(ozon_credentials())
    _scheduler_task = start_scheduler(_credentials)
    yield
    await stop_scheduler(_scheduler_task)


app = FastAPI(title="Ozon Analytics API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])


class CostIn(BaseModel):
    day: date
    kind: str = Field(pattern="^(ads|extra)$")
    amount: float = Field(gt=0)
    product_id: int | None = None
    comment: str = Field(default="", max_length=500)


class CredentialsIn(BaseModel):
    client_id: str = Field(min_length=1, max_length=100)
    api_key: str = Field(min_length=1, max_length=500)


class EconomicsIn(BaseModel):
    purchase_price: float = Field(ge=0)
    marking: float = Field(default=0, ge=0)
    packaging: float = Field(default=0, ge=0)
    inbound_delivery: float = Field(default=0, ge=0)
    cross_dock: float = Field(default=0, ge=0)
    tax_rate: float = Field(default=0.06, ge=0, le=1)
    planned_commission: float = Field(default=0, ge=0, le=1)
    planned_logistics: float = Field(default=0, ge=0)
    other_fixed: float = Field(default=0, ge=0)
    valid_from: date = Field(default_factory=date.today)


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    sku_original: str = Field(min_length=1, max_length=150)
    price: float = Field(default=0, ge=0)
    scheme: str | None = Field(default=None, pattern="^(FBO|FBS)$")


class BindingIn(BaseModel):
    target_product_id: int | None = None
    offer_id: str | None = Field(default=None, max_length=150)
    ozon_product_id: int | None = Field(default=None, gt=0)
    scheme: str | None = Field(default=None, pattern="^(FBO|FBS)$")


class NoteIn(BaseModel):
    comment: str = Field(default="", max_length=1000)


def _seller_name(info: dict) -> str | None:
    company = info.get("company")
    if isinstance(company, dict):
        return company.get("name") or company.get("legal_name")
    if isinstance(company, str) and company:
        return company
    name = info.get("name")
    return name if isinstance(name, str) else None


def _today_moscow() -> date:
    return datetime.now(MOSCOW).date()


def _resolve_day(day: date | None) -> date:
    return day or _today_moscow()


def _last_sync_finished_at() -> str | None:
    last = one(
        "SELECT finished_at FROM sync_runs WHERE status IN ('success','partial') "
        "ORDER BY id DESC LIMIT 1"
    )
    return last["finished_at"] if last else None


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "database": "connected", "mode": "ozon" if _credentials else "offline"}


@app.get("/api/dashboard")
def dashboard(day: date | None = Query(default=None)) -> dict:
    day = _resolve_day(day)
    latest_day = latest_kpi_day()
    last_synced_at = _last_sync_finished_at()
    report = daily_report(day)
    if not report["items"]:
        return {
            "day": str(day),
            "latest_day": str(latest_day) if latest_day else None,
            "last_synced_at": last_synced_at,
            "source": "ozon" if _credentials else None,
            "status": None,
            "kpi": None,
            "previous": None,
            "leaders": [],
            "empty": True,
        }
    totals = report["totals"]
    current = {**totals, "extra": totals["extra_costs"]}
    previous_report = daily_report(day - timedelta(days=1))
    previous = previous_report["totals"] if previous_report["items"] else None
    leaders = sorted(report["items"], key=lambda item: item["profit"], reverse=True)[:5]
    sources = {item["source"] for item in report["items"]}
    source = "ozon" if "ozon" in sources else "manual"
    statuses = {item["status"] for item in report["items"]}
    status = "settled" if statuses == {"settled"} else "preliminary"
    return {
        "day": str(day),
        "latest_day": str(latest_day) if latest_day else None,
        "last_synced_at": last_synced_at,
        "source": source,
        "status": status,
        "kpi": current,
        "previous": previous,
        "leaders": leaders,
        "empty": False,
    }


@app.get("/api/daily")
def daily(day: date | None = Query(default=None), search: str = "", scheme: str = "") -> dict:
    return daily_report(_resolve_day(day), search, scheme)


@app.get("/api/timeseries")
def timeseries(
    from_day: date = Query(alias="from"),
    to_day: date = Query(alias="to"),
) -> dict:
    if from_day > to_day:
        raise HTTPException(422, "Некорректный период")
    return timeseries_report(from_day, to_day)


@app.get("/api/alerts")
def alerts(
    from_day: date = Query(alias="from"),
    to_day: date = Query(alias="to"),
) -> dict:
    if from_day > to_day:
        raise HTTPException(422, "Некорректный период")
    return alerts_report(from_day, to_day)


@app.get("/api/products")
def products(search: str = "") -> dict:
    pattern = f"%{search}%"
    data = rows(
        "SELECT p.*,u.purchase_price,u.marking,u.packaging,u.inbound_delivery,u.cross_dock,u.tax_rate,"
        "u.planned_commission,u.planned_logistics,u.other_fixed,"
        "ROUND(p.price-u.purchase_price-u.marking-u.packaging-u.inbound_delivery-u.cross_dock-"
        "u.planned_commission*p.price-u.planned_logistics-u.other_fixed,2) net_per_sale "
        "FROM products p LEFT JOIN unit_economics u ON u.id=("
        "SELECT id FROM unit_economics WHERE product_id=p.id ORDER BY valid_from DESC LIMIT 1) "
        "WHERE p.name LIKE ? OR p.sku_original LIKE ? ORDER BY p.id",
        (pattern, pattern),
    )
    return {
        "items": data,
        "summary": {
            "active": len(data),
            "linked": sum(x["mapping_status"] == "linked" for x in data),
            "without_cost": sum(not x["purchase_price"] for x in data),
        },
    }


def _validate_sku(db, sku: str, product_id: int | None = None) -> str:
    normalized = normalize_sku(sku)
    duplicate = db.execute(
        "SELECT id FROM products WHERE sku_normalized=? AND (? IS NULL OR id<>?)",
        (normalized, product_id, product_id),
    ).fetchone()
    if duplicate:
        raise HTTPException(409, "Товар с таким SKU уже существует")
    return normalized


@app.post("/api/products", status_code=201)
def add_product(product: ProductIn) -> dict:
    values = product.model_dump()
    values["name"] = values["name"].strip()
    values["sku_original"] = values["sku_original"].strip()
    with connection() as db:
        normalized = _validate_sku(db, values["sku_original"])
        cursor = db.execute(
            "INSERT INTO products(sku_original,sku_normalized,name,scheme,price,mapping_status,source) "
            "VALUES(?,?,?,?,?,'unmatched','manual')",
            (values["sku_original"], normalized, values["name"], values["scheme"], values["price"]),
        )
        product_id = cursor.lastrowid
        db.execute(
            "INSERT INTO unit_economics(product_id,valid_from) VALUES(?,?)",
            (product_id, date.today().isoformat()),
        )
    return {"id": product_id, **values, "mapping_status": "unmatched", "source": "manual"}


@app.put("/api/products/{product_id}")
def update_product(product_id: int, product: ProductIn) -> dict:
    values = product.model_dump()
    values["name"] = values["name"].strip()
    values["sku_original"] = values["sku_original"].strip()
    with connection() as db:
        if not db.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
            raise HTTPException(404, "Товар не найден")
        normalized = _validate_sku(db, values["sku_original"], product_id)
        db.execute(
            "UPDATE products SET sku_original=?,sku_normalized=?,name=?,scheme=?,price=? WHERE id=?",
            (values["sku_original"], normalized, values["name"], values["scheme"], values["price"], product_id),
        )
    return {"id": product_id, **values}


@app.get("/api/ozon/catalog-products")
def ozon_catalog_products() -> list[dict]:
    return rows(
        "SELECT id,name,sku_original sku,offer_id,ozon_product_id,scheme,price,mapping_status "
        "FROM products WHERE source='ozon' OR ozon_product_id IS NOT NULL "
        "ORDER BY mapping_status='linked' DESC,name"
    )


@app.put("/api/products/{product_id}/binding")
def bind_product(product_id: int, binding: BindingIn) -> dict:
    with connection() as db:
        local = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not local:
            raise HTTPException(404, "Товар не найден")
        target = None
        if binding.target_product_id is not None:
            target = db.execute(
                "SELECT * FROM products WHERE id=? AND id<>?",
                (binding.target_product_id, product_id),
            ).fetchone()
            if not target or (target["source"] != "ozon" and target["ozon_product_id"] is None):
                raise HTTPException(404, "Товар Ozon для привязки не найден")
        offer_id = (binding.offer_id or (target["offer_id"] if target else None) or "").strip()
        ozon_product_id = binding.ozon_product_id or (target["ozon_product_id"] if target else None)
        scheme = binding.scheme or (target["scheme"] if target else None) or local["scheme"]
        if not offer_id and ozon_product_id is None:
            raise HTTPException(422, "Укажите товар Ozon или его offer_id/product_id")
        duplicate = db.execute(
            "SELECT id FROM products WHERE id NOT IN (?,?) AND mapping_status='linked' "
            "AND ((? IS NOT NULL AND ozon_product_id=?) OR (?<>'' AND offer_id=?)) LIMIT 1",
            (product_id, target["id"] if target else -1, ozon_product_id, ozon_product_id,
             offer_id, offer_id),
        ).fetchone()
        if duplicate:
            raise HTTPException(409, "Этот товар Ozon уже привязан к другому SKU")
        if target:
            conflict = db.execute(
                "SELECT 1 FROM daily_kpi source JOIN daily_kpi destination "
                "ON destination.day=source.day WHERE source.product_id=? "
                "AND destination.product_id=? LIMIT 1",
                (target["id"], product_id),
            ).fetchone()
            if conflict:
                raise HTTPException(409, "Нельзя объединить товары: у обоих уже есть данные за один день")
            db.execute("UPDATE daily_kpi SET product_id=? WHERE product_id=?", (product_id, target["id"]))
            db.execute("UPDATE manual_costs SET product_id=? WHERE product_id=?", (product_id, target["id"]))
            db.execute("UPDATE stock_snapshots SET product_id=? WHERE product_id=?", (product_id, target["id"]))
            db.execute(
                "INSERT INTO daily_notes(day,product_id,comment,author,created_at,updated_at) "
                "SELECT day,?,comment,author,created_at,updated_at FROM daily_notes WHERE product_id=? "
                "ON CONFLICT(day,product_id) DO NOTHING",
                (product_id, target["id"]),
            )
            db.execute("DELETE FROM daily_notes WHERE product_id=?", (target["id"],))
            db.execute("DELETE FROM unit_economics WHERE product_id=?", (target["id"],))
            db.execute("DELETE FROM products WHERE id=?", (target["id"],))
        db.execute(
            "UPDATE products SET offer_id=?,ozon_product_id=?,scheme=?,mapping_status='linked',"
            "price=CASE WHEN price=0 AND ? IS NOT NULL THEN ? ELSE price END WHERE id=?",
            (offer_id or None, ozon_product_id, scheme,
             target["price"] if target else None, target["price"] if target else None, product_id),
        )
    return {"product_id": product_id, "offer_id": offer_id or None,
            "ozon_product_id": ozon_product_id, "scheme": scheme, "mapping_status": "linked"}


@app.delete("/api/products/{product_id}/binding", status_code=204, response_class=Response)
def unbind_product(product_id: int) -> Response:
    with connection() as db:
        cursor = db.execute(
            "UPDATE products SET offer_id=NULL,ozon_product_id=NULL,mapping_status='unmatched' WHERE id=?",
            (product_id,),
        )
        if not cursor.rowcount:
            raise HTTPException(404, "Товар не найден")
    return Response(status_code=204)


@app.put("/api/daily/{day}/{product_id}/note")
def update_daily_note(day: date, product_id: int, note: NoteIn) -> dict:
    comment = note.comment.strip()
    with connection() as db:
        if not db.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
            raise HTTPException(404, "Товар не найден")
        if comment:
            db.execute(
                "INSERT INTO daily_notes(day,product_id,comment) VALUES(?,?,?) "
                "ON CONFLICT(day,product_id) DO UPDATE SET comment=excluded.comment,"
                "updated_at=CURRENT_TIMESTAMP",
                (str(day), product_id, comment),
            )
        else:
            db.execute("DELETE FROM daily_notes WHERE day=? AND product_id=?", (str(day), product_id))
    return {"day": str(day), "product_id": product_id, "comment": comment}


@app.put("/api/products/{product_id}/economics")
def update_economics(product_id: int, values: EconomicsIn) -> dict:
    with connection() as db:
        if not db.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
            raise HTTPException(404, "Товар не найден")
        payload = values.model_dump(mode="json")
        db.execute(
            "INSERT INTO unit_economics(product_id,valid_from,purchase_price,marking,packaging,"
            "inbound_delivery,cross_dock,tax_rate,planned_commission,planned_logistics,other_fixed) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(product_id,valid_from) DO UPDATE SET "
            "purchase_price=excluded.purchase_price,marking=excluded.marking,packaging=excluded.packaging,"
            "inbound_delivery=excluded.inbound_delivery,cross_dock=excluded.cross_dock,"
            "tax_rate=excluded.tax_rate,planned_commission=excluded.planned_commission,"
            "planned_logistics=excluded.planned_logistics,other_fixed=excluded.other_fixed",
            (product_id, payload["valid_from"], payload["purchase_price"], payload["marking"],
             payload["packaging"], payload["inbound_delivery"], payload["cross_dock"],
             payload["tax_rate"], payload["planned_commission"], payload["planned_logistics"],
             payload["other_fixed"]),
        )
    return {"product_id": product_id, **payload}


@app.get("/api/costs")
def costs(day: date | None = None) -> list[dict]:
    where, params = ("WHERE m.day=?", (str(day),)) if day else ("", ())
    return rows(
        f"SELECT m.*,p.name product,p.sku_original sku FROM manual_costs m "
        f"LEFT JOIN products p ON p.id=m.product_id {where} ORDER BY m.created_at DESC",
        params,
    )


@app.post("/api/costs", status_code=201)
def add_cost(cost: CostIn) -> dict:
    with connection() as db:
        if cost.product_id and not db.execute("SELECT 1 FROM products WHERE id=?", (cost.product_id,)).fetchone():
            raise HTTPException(404, "Товар не найден")
        cursor = db.execute(
            "INSERT INTO manual_costs(day,kind,product_id,amount,comment) VALUES(?,?,?,?,?)",
            (str(cost.day), cost.kind, cost.product_id, cost.amount, cost.comment.strip()),
        )
        return {"id": cursor.lastrowid, **cost.model_dump(mode="json")}


@app.put("/api/costs/{cost_id}")
def update_cost(cost_id: int, cost: CostIn) -> dict:
    with connection() as db:
        if cost.product_id and not db.execute("SELECT 1 FROM products WHERE id=?", (cost.product_id,)).fetchone():
            raise HTTPException(404, "Товар не найден")
        cursor = db.execute(
            "UPDATE manual_costs SET day=?,kind=?,product_id=?,amount=?,comment=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (str(cost.day), cost.kind, cost.product_id, cost.amount, cost.comment.strip(), cost_id),
        )
        if not cursor.rowcount:
            raise HTTPException(404, "Расход не найден")
    return {"id": cost_id, **cost.model_dump(mode="json")}


@app.delete("/api/costs/{cost_id}", status_code=204, response_class=Response)
def delete_cost(cost_id: int) -> Response:
    with connection() as db:
        if not db.execute("DELETE FROM manual_costs WHERE id=? RETURNING id", (cost_id,)).fetchone():
            raise HTTPException(404, "Расход не найден")
    return Response(status_code=204)


@app.get("/api/ozon/status")
def ozon_status() -> dict:
    last = one("SELECT task,status,finished_at,error,records,window_from,window_to FROM sync_runs ORDER BY id DESC LIMIT 1")
    return {
        "connected": bool(_credentials),
        "client_id": _credentials.get("client_id"),
        "api_key": None,
        "last_run": last,
        **scheduler_status(),
    }


@app.post("/api/ozon/connect")
def connect_ozon(credentials: CredentialsIn) -> dict:
    try:
        info = OzonClient(credentials.client_id, credentials.api_key).check()
    except OzonError as exc:
        raise HTTPException(502, str(exc)) from exc
    _credentials.update(client_id=credentials.client_id, api_key=credentials.api_key)
    return {"connected": True, "client_id": credentials.client_id, "api_key": None, "company": _seller_name(info)}


@app.post("/api/ozon/sync", status_code=202)
def sync_ozon(
    fast: bool = Query(default=True),
    from_day: date | None = Query(default=None, alias="from"),
    to_day: date | None = Query(default=None, alias="to"),
) -> dict:
    """Kick off a read-only sync in the background and return immediately.

    Progress is observed via /api/ozon/status (running/last_run); the
    frontend polls it. `fast` (default) refreshes just the last two days for
    quick manual "текущий контроль" checks; without it a full incremental
    catch-up runs; an explicit from/to overrides both for one-off backfills.
    """
    if not _credentials:
        raise HTTPException(409, "Сначала подключите кабинет Ozon")
    now = datetime.now(timezone.utc)
    if from_day and to_day:
        if from_day > to_day:
            raise HTTPException(422, "Некорректный период")
        window_from = moscow_day_bounds(from_day)[0]
        window_to = moscow_day_bounds(to_day)[1]
        task = "manual-range"
    elif fast:
        window_from, window_to = now - timedelta(days=FAST_SYNC_LOOKBACK_DAYS), now
        task = "fast"
    else:
        window_from, window_to = sync_window(now)
        task = "full"
    try:
        run_id = begin_sync_run(task, window_from, window_to)
    except SyncBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    threading.Thread(
        target=execute_sync_run,
        args=(run_id, dict(_credentials), window_from, window_to),
        daemon=True,
    ).start()
    return {"status": "started", "run_id": run_id, "task": task}


@app.get("/api/sync-runs")
def sync_runs() -> list[dict]:
    return rows("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 5")


@app.get("/api/export.xlsx")
def export_excel(day: date | None = Query(default=None)):
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(503, "Для экспорта установите openpyxl") from exc
    selected = _resolve_day(day)
    data = daily_report(selected)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = str(selected)
    headers = ["Товар", "SKU", "Схема", "Продано", "Выручка", "Реклама", "ДРР", "Доп. расходы",
               "Чистая прибыль", "Маржа", "Статус"]
    sheet.append(headers)
    for item in data["items"]:
        sheet.append([
            item["name"], item["sku"], item["scheme"], item["sold"], item["revenue"], item["ads"],
            item["drr"], item["extra_costs"], item["profit"], item["margin"], item["status"],
        ])
    common = data["unallocated_costs"]
    if common["ads"] or common["extra"]:
        sheet.append([
            "Общий расход дня", "—", "—", 0, 0, common["ads"], None, common["extra"],
            -(common["ads"] + common["extra"]), None, "manual",
        ])
    totals = data["totals"]
    sheet.append([
        "Итого", f'{totals["sku_count"]} SKU', "", totals["sold"], totals["revenue"], totals["ads"],
        totals["drr"], totals["extra_costs"], totals["profit"], totals["margin"], "",
    ])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="ozon-analytics-{selected}.xlsx"'},
    )
