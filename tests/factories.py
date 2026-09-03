from __future__ import annotations

from datetime import date

from backend.database import connection
from backend.skus import normalize_sku


def make_product(
    *,
    name: str = "Товар",
    sku: str = "sku-1",
    source: str = "manual",
    mapping_status: str = "unmatched",
    price: float = 1000,
    purchase_price: float = 200,
    scheme: str = "FBO",
    offer_id: str | None = None,
    ozon_product_id: int | None = None,
) -> int:
    with connection() as db:
        cursor = db.execute(
            "INSERT INTO products(sku_original,sku_normalized,name,offer_id,ozon_product_id,"
            "scheme,price,mapping_status,source) VALUES(?,?,?,?,?,?,?,?,?)",
            (sku, normalize_sku(sku), name, offer_id, ozon_product_id, scheme, price,
             mapping_status, source),
        )
        product_id = cursor.lastrowid
        db.execute(
            "INSERT INTO unit_economics(product_id,valid_from,purchase_price) VALUES(?,?,?)",
            (product_id, date.today().isoformat(), purchase_price),
        )
        return product_id


def make_kpi(
    product_id: int,
    day: str,
    *,
    sold: int = 10,
    revenue: float = 10000,
    ads: float = 0,
    extra_costs: float = 0,
    profit: float = 5000,
    status: str = "settled",
    source: str = "ozon",
) -> None:
    with connection() as db:
        db.execute(
            "INSERT INTO daily_kpi(day,product_id,sold,revenue,ads,extra_costs,profit,status,source) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (day, product_id, sold, revenue, ads, extra_costs, profit, status, source),
        )


def make_sync_run(
    *,
    task: str = "full",
    status: str = "success",
    records: int = 1,
    error: str | None = None,
    window_from: str | None = None,
    window_to: str | None = None,
    finished_at: str | None = None,
) -> int:
    with connection() as db:
        cursor = db.execute(
            "INSERT INTO sync_runs(task,status,records,error,window_from,window_to,finished_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (task, status, records, error, window_from, window_to, finished_at),
        )
        return cursor.lastrowid


def seed_sample() -> dict[str, int]:
    first = make_product(name="Длинные мужские Белые", sku="а3", source="ozon",
                         mapping_status="linked", purchase_price=268, offer_id="а3")
    second = make_product(name="Носки укороченные 5 пар", sku="0 5-1", source="manual",
                          mapping_status="unmatched", purchase_price=0)
    make_kpi(first, "2026-07-01", sold=20, revenue=20000, ads=2000, extra_costs=200, profit=4000)
    make_kpi(first, "2026-07-02", sold=15, revenue=15000, ads=1500, extra_costs=150, profit=3000)
    make_kpi(second, "2026-07-01", sold=10, revenue=8000, ads=500, extra_costs=50, profit=-200)
    make_kpi(second, "2026-07-02", sold=8, revenue=6400, ads=1600, extra_costs=80, profit=-400)
    return {"first": first, "second": second}
