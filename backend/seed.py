from __future__ import annotations

import re
import unicodedata

from .database import connection, init_db

NAMES = [
    ("а3", "Длинные мужские Белые"), ("с1", "Мужские носки набор 10 пар"),
    ("р2", "Носки мужские спортивные"), ("0 1", "Носки короткие белые"),
    ("б4", "Длинные мужские Чёрные"), ("м7", "Носки базовые серые"),
    ("0 5-1", "Носки укороченные 5 пар"),
] + [(f"sku-{i:02}", f"Носки мужские, модель {i:02}") for i in range(8, 35)]


def normalize_sku(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    return re.sub(r"\s+", "", value)


def _spread(total: float, weights: list[int]) -> list[float]:
    values = [round(total * weight / sum(weights), 2) for weight in weights]
    values[-1] = round(values[-1] + total - sum(values), 2)
    return values


def seed_demo(force: bool = False) -> None:
    """Create a deterministic 34-SKU demo with exact workbook control totals."""
    init_db()
    with connection() as db:
        if db.execute("SELECT COUNT(*) FROM products").fetchone()[0] and not force:
            return
        if force:
            for table in ("manual_costs", "daily_kpi", "unit_economics", "products", "import_runs"):
                db.execute(f"DELETE FROM {table}")
        for idx, (sku, name) in enumerate(NAMES, 1):
            db.execute(
                "INSERT INTO products(id,sku_original,sku_normalized,name,offer_id,scheme,price,mapping_status,source) VALUES(?,?,?,?,?,?,?,?,?)",
                (idx, sku, normalize_sku(sku), name, sku if idx <= 32 else None,
                 "FBO" if idx % 4 else "FBS", 799 + (idx % 5) * 40,
                 "linked" if idx <= 32 else "unmatched", "excel_demo"),
            )
            db.execute(
                "INSERT INTO unit_economics(product_id,valid_from,purchase_price,marking,packaging,inbound_delivery,cross_dock,tax_rate,planned_commission,planned_logistics,other_fixed) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (idx, "2026-07-01", 0 if idx == 7 else 235 + idx * 2, 5, 18, 12, 7, .06, .20, 95, 4),
            )

        for day, sold_total, revenue_total, ads_total, extra_total, profit_total in (
            ("2026-07-01", 690, 649721.15, 88307, 18729, 25378.69),
            ("2026-07-02", 678, 627729.15, 81876, 23010, 22740.55),
        ):
            weights = [35 + ((i * 17) % 31) for i in range(34)]
            sold = [round(sold_total * w / sum(weights)) for w in weights]
            sold[-1] += sold_total - sum(sold)
            revenue = _spread(revenue_total, weights)
            ads = _spread(ads_total, weights)
            extra = _spread(extra_total, weights)
            profit = _spread(profit_total, weights)
            if day == "2026-07-02":
                delta = 2940.81 - profit[0]
                profit[0] = 2940.81
                profit[-1] = round(profit[-1] - delta, 2)
            for i in range(34):
                db.execute(
                    "INSERT INTO daily_kpi(day,product_id,sold,revenue,ads,extra_costs,profit,status,source) VALUES(?,?,?,?,?,?,?,?,?)",
                    (day, i + 1, sold[i], revenue[i], ads[i], extra[i], profit[i], "demo", "excel_demo"),
                )
        db.execute(
            "INSERT INTO import_runs(filename,status,products,rows,details) VALUES(?,?,?,?,?)",
            ("Носки_V2 (1).xlsx", "success", 34, 68, "Контрольные итоги 1–2 июля проверены"),
        )
