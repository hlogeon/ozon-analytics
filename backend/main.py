from __future__ import annotations

import io
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .database import connection, init_db, json_dump, one, rows
from .ozon import OzonClient, OzonError
from .seed import normalize_sku, seed_demo

MOSCOW = ZoneInfo("Europe/Moscow")

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


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _day_in_moscow(value: str | None) -> str:
    if not value:
        return datetime.now(MOSCOW).date().isoformat()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(MOSCOW).date().isoformat()
    except ValueError:
        return value[:10]


def _upsert_product(db, item: dict, scheme: str | None = None) -> int:
    offer = str(item.get("offer_id") or item.get("sku") or "").strip()
    if not offer:
        raise ValueError("Ozon вернул товар без offer_id")
    normalized = normalize_sku(offer)
    product_id = item.get("product_id") or item.get("sku")
    existing = db.execute(
        "SELECT id FROM products WHERE (? IS NOT NULL AND ozon_product_id=?) OR offer_id=? "
        "OR sku_normalized=? ORDER BY mapping_status='linked' DESC,source<>'ozon' DESC LIMIT 1",
        (product_id, product_id, offer, normalized),
    ).fetchone()
    name = str(item.get("name") or offer)
    price = _number(item.get("price"))
    if existing:
        db.execute(
            "UPDATE products SET offer_id=?,ozon_product_id=COALESCE(?,ozon_product_id),"
            "name=CASE WHEN source='ozon' AND ?<>? THEN ? ELSE name END,scheme=COALESCE(?,scheme),"
            "price=CASE WHEN ?>0 THEN ? ELSE price END,mapping_status='linked' WHERE id=?",
            (offer, product_id, name, offer, name, scheme, price, price, existing["id"]),
        )
        return existing["id"]
    cursor = db.execute(
        "INSERT INTO products(sku_original,sku_normalized,name,offer_id,ozon_product_id,"
        "scheme,price,mapping_status,source) VALUES(?,?,?,?,?,?,?,?,?)",
        (offer, normalized, name, offer, product_id, scheme, price, "linked", "ozon"),
    )
    new_id = cursor.lastrowid
    db.execute(
        "INSERT INTO unit_economics(product_id,valid_from) VALUES(?,?)",
        (new_id, date.today().isoformat()),
    )
    return new_id


def _posting_fees(posting: dict) -> tuple[dict[str, float], float]:
    finance = posting.get("financial_data") or {}
    by_offer: dict[str, float] = {}
    for item in finance.get("products") or []:
        fee = abs(_number(item.get("commission_amount")))
        fee += sum(abs(_number(value)) for value in (item.get("item_services") or {}).values())
        identifiers = {item.get("offer_id"), item.get("product_id")}
        for identifier in identifiers - {None, ""}:
            key = normalize_sku(identifier)
            by_offer[key] = by_offer.get(key, 0) + fee
    common = sum(abs(_number(value)) for value in (finance.get("posting_services") or {}).values())
    return by_offer, common


def _rebuild_live_kpi(db, since_day: str, to_day: str) -> int:
    db.execute(
        "DELETE FROM daily_kpi WHERE source='ozon' AND day BETWEEN ? AND ?",
        (since_day, to_day),
    )
    aggregates: dict[tuple[str, int], dict] = {}
    postings = db.execute(
        "SELECT scheme,status,occurred_at,raw_json FROM ozon_postings "
        "WHERE substr(occurred_at,1,10) BETWEEN ? AND ?",
        (since_day, to_day),
    ).fetchall()
    for stored in postings:
        posting = json.loads(stored["raw_json"])
        status = (posting.get("status") or stored["status"] or "").casefold()
        day = _day_in_moscow(stored["occurred_at"])
        products = posting.get("products") or []
        posting_revenue = sum(
            _number(item.get("price")) * int(item.get("quantity") or 1) for item in products
        )
        fees_by_offer, common_fees = _posting_fees(posting)
        settled = bool(posting.get("financial_data"))
        for item in products:
            product_id = _upsert_product(db, item, stored["scheme"])
            quantity = int(item.get("quantity") or 1)
            cancelled = quantity if "cancel" in status else 0
            returned = quantity if "return" in status else 0
            sold = 0 if cancelled or returned else quantity
            revenue = _number(item.get("price")) * sold
            offer = normalize_sku(item.get("offer_id") or "")
            sku = normalize_sku(item.get("sku") or item.get("product_id") or "")
            fees = fees_by_offer.get(offer, fees_by_offer.get(sku, 0))
            if posting_revenue:
                fees += common_fees * revenue / posting_revenue
            economics = db.execute(
                "SELECT * FROM unit_economics WHERE product_id=? "
                "ORDER BY valid_from DESC LIMIT 1",
                (product_id,),
            ).fetchone()
            unit_cost = sum(
                economics[key]
                for key in ("purchase_price", "marking", "packaging", "inbound_delivery", "cross_dock", "other_fixed")
            )
            cogs = unit_cost * sold
            tax = revenue * economics["tax_rate"]
            if not settled:
                fees = revenue * economics["planned_commission"] + sold * economics["planned_logistics"]
            key = (day, product_id)
            row = aggregates.setdefault(
                key,
                {"sold": 0, "returns": 0, "cancelled": 0, "revenue": 0.0,
                 "ozon_fees": 0.0, "cogs": 0.0, "tax": 0.0, "settled": True},
            )
            row["sold"] += sold
            row["returns"] += returned
            row["cancelled"] += cancelled
            row["revenue"] += revenue
            row["ozon_fees"] += fees
            row["cogs"] += cogs
            row["tax"] += tax
            row["settled"] = row["settled"] and settled
    for (day, product_id), item in aggregates.items():
        profit = item["revenue"] - item["ozon_fees"] - item["cogs"] - item["tax"]
        db.execute(
            "INSERT INTO daily_kpi(day,product_id,sold,returns,cancelled,revenue,ozon_fees,"
            "cogs,tax,profit,status,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (day, product_id, item["sold"], item["returns"], item["cancelled"],
             round(item["revenue"], 2), round(item["ozon_fees"], 2),
             round(item["cogs"], 2), round(item["tax"], 2), round(profit, 2),
             "settled" if item["settled"] else "preliminary", "ozon"),
        )
    return len(aggregates)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "database": "connected", "mode": "ozon" if _credentials else "excel_demo"}


@app.get("/api/dashboard")
def dashboard(day: date | None = Query(default=None)) -> dict:
    if day is None:
        latest = one("SELECT MAX(day) day FROM daily_kpi")
        day = date.fromisoformat(latest["day"]) if latest and latest["day"] else date(2026, 7, 2)
    report = daily(day)
    if not report["items"]:
        raise HTTPException(404, "За выбранную дату данных нет")
    totals = report["totals"]
    current = {**totals, "extra": totals["extra_costs"]}
    previous_report = daily(day - timedelta(days=1))
    previous = previous_report["totals"] if previous_report["items"] else None
    leaders = sorted(report["items"], key=lambda item: item["profit"], reverse=True)[:5]
    sources = {item["source"] for item in report["items"]}
    source = "ozon" if "ozon" in sources else "excel_demo"
    statuses = {item["status"] for item in report["items"]}
    status = "demo" if source == "excel_demo" else ("settled" if statuses == {"settled"} else "preliminary")
    return {"day": str(day), "source": source, "status": status, "kpi": current, "previous": previous, "leaders": leaders}


@app.get("/api/daily")
def daily(day: date = Query(default=date(2026, 7, 2)), search: str = "", scheme: str = "") -> dict:
    clauses, params = ["k.day=?"], [str(day)]
    if search:
        clauses.append("(p.name LIKE ? OR p.sku_original LIKE ? OR p.sku_normalized LIKE ?)")
        params += [f"%{search}%"] * 3
    if scheme in ("FBO", "FBS"):
        clauses.append("p.scheme=?"); params.append(scheme)
    data = rows(f"SELECT p.id product_id,p.name,p.sku_original sku,p.scheme,k.sold,k.returns,k.cancelled,k.revenue,k.ads,k.extra_costs,k.profit,k.status,k.source,COALESCE((SELECT comment FROM daily_notes n WHERE n.day=k.day AND n.product_id=k.product_id),'') note FROM daily_kpi k JOIN products p ON p.id=k.product_id WHERE {' AND '.join(clauses)} ORDER BY k.profit DESC", tuple(params))
    product_costs = rows(
        "SELECT product_id,kind,SUM(amount) amount FROM manual_costs "
        "WHERE day=? AND product_id IS NOT NULL GROUP BY product_id,kind",
        (str(day),),
    )
    cost_map = {(item["product_id"], item["kind"]): item["amount"] for item in product_costs}
    for item in data:
        manual_ads = cost_map.get((item["product_id"], "ads"), 0)
        manual_extra = cost_map.get((item["product_id"], "extra"), 0)
        item["ads"] = round(item["ads"] + manual_ads, 2)
        item["extra_costs"] = round(item["extra_costs"] + manual_extra, 2)
        item["profit"] = round(item["profit"] - manual_ads - manual_extra, 2)
        item["drr"] = round(item["ads"] / item["revenue"] * 100, 2) if item["revenue"] else None
        item["margin"] = round(item["profit"] / item["revenue"] * 100, 2) if item["revenue"] else None
    totals = {key: round(sum((item[key] or 0) for item in data), 2) for key in ("sold", "revenue", "ads", "extra_costs", "profit")}
    unallocated = rows(
        "SELECT kind,COALESCE(SUM(amount),0) amount FROM manual_costs "
        "WHERE day=? AND product_id IS NULL GROUP BY kind",
        (str(day),),
    )
    unallocated_map = {item["kind"]: item["amount"] for item in unallocated}
    totals["ads"] = round(totals["ads"] + unallocated_map.get("ads", 0), 2)
    totals["extra_costs"] = round(totals["extra_costs"] + unallocated_map.get("extra", 0), 2)
    totals["profit"] = round(totals["profit"] - sum(unallocated_map.values()), 2)
    totals.update(sku_count=len(data), drr=round(totals["ads"] / totals["revenue"] * 100, 2) if totals["revenue"] else None, margin=round(totals["profit"] / totals["revenue"] * 100, 2) if totals["revenue"] else None)
    return {"items": data, "totals": totals, "unallocated_costs": {"ads": unallocated_map.get("ads", 0), "extra": unallocated_map.get("extra", 0)}}


@app.get("/api/products")
def products(search: str = "") -> dict:
    pattern = f"%{search}%"
    data = rows("SELECT p.*,u.purchase_price,u.marking,u.packaging,u.inbound_delivery,u.cross_dock,u.tax_rate,u.planned_commission,u.planned_logistics,u.other_fixed,ROUND(p.price-u.purchase_price-u.marking-u.packaging-u.inbound_delivery-u.cross_dock-u.planned_commission*p.price-u.planned_logistics-u.other_fixed,2) net_per_sale FROM products p LEFT JOIN unit_economics u ON u.id=(SELECT id FROM unit_economics WHERE product_id=p.id ORDER BY valid_from DESC LIMIT 1) WHERE p.name LIKE ? OR p.sku_original LIKE ? ORDER BY p.id", (pattern, pattern))
    return {"items": data, "summary": {"active": len(data), "linked": sum(x["mapping_status"] == "linked" for x in data), "without_cost": sum(not x["purchase_price"] for x in data)}}


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
    return rows(f"SELECT m.*,p.name product,p.sku_original sku FROM manual_costs m LEFT JOIN products p ON p.id=m.product_id {where} ORDER BY m.created_at DESC", params)


@app.post("/api/costs", status_code=201)
def add_cost(cost: CostIn) -> dict:
    with connection() as db:
        if cost.product_id and not db.execute("SELECT 1 FROM products WHERE id=?", (cost.product_id,)).fetchone():
            raise HTTPException(404, "Товар не найден")
        cursor = db.execute("INSERT INTO manual_costs(day,kind,product_id,amount,comment) VALUES(?,?,?,?,?)", (str(cost.day), cost.kind, cost.product_id, cost.amount, cost.comment.strip()))
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
                _upsert_product(db, item)
            for scheme, payload in (("FBS", client.fbs_postings(since.isoformat(), now.isoformat())), ("FBO", client.fbo_postings(since.isoformat(), now.isoformat()))):
                result = payload.get("result") or []
                postings = result.get("postings", []) if isinstance(result, dict) else result
                for posting in postings:
                    number = posting.get("posting_number") or posting.get("order_number")
                    if not number:
                        continue
                    for item in posting.get("products") or []:
                        _upsert_product(db, item, scheme)
                    db.execute("INSERT INTO ozon_postings(posting_number,scheme,status,occurred_at,raw_json) VALUES(?,?,?,?,?) ON CONFLICT(posting_number) DO UPDATE SET status=excluded.status,raw_json=excluded.raw_json,updated_at=CURRENT_TIMESTAMP", (number, scheme, posting.get("status"), posting.get("in_process_at") or posting.get("created_at"), json_dump(posting)))
                    count += 1
            finance_payload = client.finance(since.isoformat(), now.isoformat())
            operations = finance_payload.get("result", {}).get("operations", [])
            for operation in operations:
                operation_id = str(operation.get("operation_id") or operation.get("id") or "")
                if not operation_id:
                    continue
                db.execute(
                    "INSERT INTO ozon_finance_operations(operation_id,occurred_at,operation_type,amount,raw_json) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET occurred_at=excluded.occurred_at,"
                    "operation_type=excluded.operation_type,amount=excluded.amount,raw_json=excluded.raw_json",
                    (operation_id, operation.get("operation_date") or operation.get("created_at"),
                     operation.get("operation_type"), _number(operation.get("amount")), json_dump(operation)),
                )
            kpi_rows = _rebuild_live_kpi(db, since.date().isoformat(), now.date().isoformat())
            records = count + len(product_items) + len(operations) + kpi_rows
            db.execute("UPDATE sync_runs SET status='success',finished_at=CURRENT_TIMESTAMP,records=? WHERE id=?", (records, run))
    except Exception as exc:
        with connection() as db:
            db.execute("UPDATE sync_runs SET status='error',finished_at=CURRENT_TIMESTAMP,error=? WHERE id=?", (str(exc)[:500], run))
        raise HTTPException(502, str(exc)) from exc
    latest = one("SELECT MAX(day) day FROM daily_kpi WHERE source='ozon'")
    return {"status": "success", "records": records, "latest_day": latest["day"] if latest else None}


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
    common = data["unallocated_costs"]
    if common["ads"] or common["extra"]:
        sheet.append(["Общий расход дня", "—", "—", 0, 0, common["ads"], None, common["extra"], -(common["ads"] + common["extra"]), None, "manual"])
    totals = data["totals"]
    sheet.append(["Итого", f'{totals["sku_count"]} SKU', "", totals["sold"], totals["revenue"], totals["ads"], totals["drr"], totals["extra_costs"], totals["profit"], totals["margin"], ""])
    sheet.freeze_panes = "A2"; sheet.auto_filter.ref = sheet.dimensions
    output = io.BytesIO(); workbook.save(output); output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="ozon-analytics-{day}.xlsx"'})
