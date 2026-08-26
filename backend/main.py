from __future__ import annotations

import io
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .database import connection, init_db, json_dump, one, rows
from .ozon import OzonClient, OzonError
from .seed import seed_demo

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    seed_demo()
    yield


app = FastAPI(title="Ozon Analytics API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])
_credentials: dict[str, str] = {}


class CostIn(BaseModel):
    day: date
    kind: str = Field(pattern="^(ads|extra)$")
    amount: float = Field(gt=0)
    product_id: int | None = None
    comment: str = Field(default="", max_length=500)


class CredentialsIn(BaseModel):
    client_id: str = Field(min_length=1, max_length=100)
    api_key: str = Field(min_length=1, max_length=500)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "database": "connected", "mode": "ozon" if _credentials else "excel_demo"}


@app.get("/api/dashboard")
def dashboard(day: date = Query(default=date(2026, 7, 2))) -> dict:
    current = one("SELECT SUM(sold) sold,SUM(revenue) revenue,SUM(ads) ads,SUM(extra_costs) extra,SUM(profit) profit FROM daily_kpi WHERE day=?", (str(day),))
    if not current or current["revenue"] is None:
        raise HTTPException(404, "За выбранную дату данных нет")
    previous = one("SELECT SUM(sold) sold,SUM(revenue) revenue,SUM(ads) ads,SUM(extra_costs) extra,SUM(profit) profit FROM daily_kpi WHERE day=?", (str(day - timedelta(days=1)),))
    manual = one("SELECT COALESCE(SUM(amount),0) total FROM manual_costs WHERE day=?", (str(day),))["total"]
    current["profit"] = round(current["profit"] - manual, 2)
    current["extra"] = round(current["extra"] + manual, 2)
    current["margin"] = round(current["profit"] / current["revenue"] * 100, 2) if current["revenue"] else None
    current["drr"] = round(current["ads"] / current["revenue"] * 100, 2) if current["revenue"] else None
    leaders = rows("SELECT p.id,p.name,p.sku_original sku,k.sold,k.profit,ROUND(k.profit/k.revenue*100,2) margin FROM daily_kpi k JOIN products p ON p.id=k.product_id WHERE k.day=? ORDER BY k.profit DESC LIMIT 5", (str(day),))
    return {"day": str(day), "source": "excel_demo", "status": "demo", "kpi": current, "previous": previous, "leaders": leaders}


@app.get("/api/daily")
def daily(day: date = Query(default=date(2026, 7, 2)), search: str = "", scheme: str = "") -> dict:
    clauses, params = ["k.day=?"], [str(day)]
    if search:
        clauses.append("(p.name LIKE ? OR p.sku_original LIKE ? OR p.sku_normalized LIKE ?)")
        params += [f"%{search}%"] * 3
    if scheme in ("FBO", "FBS"):
        clauses.append("p.scheme=?"); params.append(scheme)
    data = rows(f"SELECT p.id product_id,p.name,p.sku_original sku,p.scheme,k.sold,k.returns,k.cancelled,k.revenue,k.ads,k.extra_costs,k.profit,k.status,CASE WHEN k.revenue=0 THEN NULL ELSE ROUND(k.ads/k.revenue*100,2) END drr,CASE WHEN k.revenue=0 THEN NULL ELSE ROUND(k.profit/k.revenue*100,2) END margin FROM daily_kpi k JOIN products p ON p.id=k.product_id WHERE {' AND '.join(clauses)} ORDER BY k.profit DESC", tuple(params))
    totals = {key: round(sum((item[key] or 0) for item in data), 2) for key in ("sold", "revenue", "ads", "extra_costs", "profit")}
    totals.update(sku_count=len(data), drr=round(totals["ads"] / totals["revenue"] * 100, 2) if totals["revenue"] else None, margin=round(totals["profit"] / totals["revenue"] * 100, 2) if totals["revenue"] else None)
    return {"items": data, "totals": totals}


@app.get("/api/products")
def products(search: str = "") -> dict:
    pattern = f"%{search}%"
    data = rows("SELECT p.*,u.purchase_price,u.marking,u.packaging,u.inbound_delivery,u.cross_dock,u.tax_rate,u.planned_commission,u.planned_logistics,u.other_fixed,ROUND(p.price-u.purchase_price-u.marking-u.packaging-u.inbound_delivery-u.cross_dock-u.planned_commission*p.price-u.planned_logistics-u.other_fixed,2) net_per_sale FROM products p JOIN unit_economics u ON u.product_id=p.id WHERE p.name LIKE ? OR p.sku_original LIKE ? ORDER BY p.id", (pattern, pattern))
    return {"items": data, "summary": {"active": len(data), "linked": sum(x["mapping_status"] == "linked" for x in data), "without_cost": sum(not x["purchase_price"] for x in data)}}


@app.get("/api/costs")
def costs(day: date | None = None) -> list[dict]:
    where, params = ("WHERE m.day=?", (str(day),)) if day else ("", ())
    return rows(f"SELECT m.*,p.name product,p.sku_original sku FROM manual_costs m LEFT JOIN products p ON p.id=m.product_id {where} ORDER BY m.created_at DESC", params)


@app.post("/api/costs", status_code=201)
def add_cost(cost: CostIn) -> dict:
    with connection() as db:
        if cost.product_id and not db.execute("SELECT 1 FROM products WHERE id=?", (cost.product_id,)).fetchone():
            raise HTTPException(404, "Товар не найден")
        cursor = db.execute("INSERT INTO manual_costs(day,kind,product_id,amount,comment) VALUES(?,?,?,?,?)", (str(cost.day), cost.kind, cost.product_id, cost.amount, cost.comment.strip()))
        return {"id": cursor.lastrowid, **cost.model_dump(mode="json")}


@app.delete("/api/costs/{cost_id}", status_code=204, response_class=Response)
def delete_cost(cost_id: int) -> Response:
    with connection() as db:
        if not db.execute("DELETE FROM manual_costs WHERE id=? RETURNING id", (cost_id,)).fetchone():
            raise HTTPException(404, "Расход не найден")
    return Response(status_code=204)


@app.get("/api/ozon/status")
def ozon_status() -> dict:
    last = one("SELECT task,status,finished_at,error,records FROM sync_runs ORDER BY id DESC LIMIT 1")
    return {"connected": bool(_credentials), "client_id": _credentials.get("client_id"), "api_key": None, "last_run": last}


@app.post("/api/ozon/connect")
def connect_ozon(credentials: CredentialsIn) -> dict:
    try:
        info = OzonClient(credentials.client_id, credentials.api_key).check()
    except OzonError as exc:
        raise HTTPException(502, str(exc)) from exc
    _credentials.update(client_id=credentials.client_id, api_key=credentials.api_key)
    return {"connected": True, "client_id": credentials.client_id, "api_key": None, "company": info.get("name") or info.get("company")}


@app.post("/api/ozon/sync")
def sync_ozon() -> dict:
    if not _credentials:
        raise HTTPException(409, "Сначала подключите кабинет Ozon")
    now = datetime.now(timezone.utc); since = now - timedelta(days=14)
    with connection() as db:
        run = db.execute("INSERT INTO sync_runs(task,status) VALUES('full','running')").lastrowid
    count = 0
    try:
        client = OzonClient(**_credentials)
        product_payload = client.products()
        product_items = product_payload.get("result", {}).get("items", [])
        with connection() as db:
            for item in product_items:
                offer = str(item.get("offer_id", ""))
                db.execute("UPDATE products SET ozon_product_id=?,mapping_status='linked' WHERE sku_normalized=REPLACE(LOWER(?),' ','')", (item.get("product_id"), offer))
            for scheme, payload in (("FBS", client.fbs_postings(since.isoformat(), now.isoformat())), ("FBO", client.fbo_postings(since.isoformat(), now.isoformat()))):
                postings = payload.get("result", {}).get("postings", payload.get("result", []))
                for posting in postings:
                    number = posting.get("posting_number") or posting.get("order_number")
                    db.execute("INSERT INTO ozon_postings(posting_number,scheme,status,occurred_at,raw_json) VALUES(?,?,?,?,?) ON CONFLICT(posting_number) DO UPDATE SET status=excluded.status,raw_json=excluded.raw_json,updated_at=CURRENT_TIMESTAMP", (number, scheme, posting.get("status"), posting.get("in_process_at") or posting.get("created_at"), json_dump(posting)))
                    count += 1
            db.execute("UPDATE sync_runs SET status='success',finished_at=CURRENT_TIMESTAMP,records=? WHERE id=?", (count + len(product_items), run))
    except Exception as exc:
        with connection() as db:
            db.execute("UPDATE sync_runs SET status='error',finished_at=CURRENT_TIMESTAMP,error=? WHERE id=?", (str(exc)[:500], run))
        raise HTTPException(502, str(exc)) from exc
    return {"status": "success", "records": count + len(product_items)}


@app.get("/api/sync-runs")
def sync_runs() -> list[dict]:
    return rows("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 5")


@app.get("/api/export.xlsx")
def export_excel(day: date = Query(default=date(2026, 7, 2))):
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(503, "Для экспорта установите openpyxl") from exc
    data = daily(day)
    workbook = Workbook(); sheet = workbook.active; sheet.title = str(day)
    headers = ["Товар", "SKU", "Схема", "Продано", "Выручка", "Реклама", "ДРР", "Доп. расходы", "Чистая прибыль", "Маржа", "Статус"]
    sheet.append(headers)
    for item in data["items"]:
        sheet.append([item["name"], item["sku"], item["scheme"], item["sold"], item["revenue"], item["ads"], item["drr"], item["extra_costs"], item["profit"], item["margin"], item["status"]])
    sheet.freeze_panes = "A2"; sheet.auto_filter.ref = sheet.dimensions
    output = io.BytesIO(); workbook.save(output); output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="ozon-analytics-{day}.xlsx"'})
