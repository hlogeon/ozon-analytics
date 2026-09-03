from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .config import stale_after_hours
from .database import one, rows

DRR_THRESHOLD = 0.20
SEVERITY_ORDER = {"red": 0, "orange": 1, "yellow": 2, "blue": 3}


def latest_kpi_day() -> date | None:
    latest = one("SELECT MAX(day) day FROM daily_kpi")
    if latest and latest["day"]:
        return date.fromisoformat(latest["day"])
    return None


def _ru_count(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        word = one
    elif n10 in (2, 3, 4) and n100 not in (12, 13, 14):
        word = few
    else:
        word = many
    return f"{n} {word}"


def apply_manual_costs(items: list[dict], day: str) -> tuple[list[dict], dict, dict]:
    product_costs = rows(
        "SELECT product_id,kind,SUM(amount) amount FROM manual_costs "
        "WHERE day=? AND product_id IS NOT NULL GROUP BY product_id,kind",
        (day,),
    )
    cost_map = {(item["product_id"], item["kind"]): item["amount"] for item in product_costs}
    for item in items:
        manual_ads = cost_map.get((item["product_id"], "ads"), 0)
        manual_extra = cost_map.get((item["product_id"], "extra"), 0)
        item["ads"] = round(item["ads"] + manual_ads, 2)
        item["extra_costs"] = round(item["extra_costs"] + manual_extra, 2)
        item["profit"] = round(item["profit"] - manual_ads - manual_extra, 2)
        item["drr"] = round(item["ads"] / item["revenue"] * 100, 2) if item["revenue"] else None
        item["margin"] = round(item["profit"] / item["revenue"] * 100, 2) if item["revenue"] else None
    totals = {
        key: round(sum((item[key] or 0) for item in items), 2)
        for key in ("sold", "revenue", "ads", "extra_costs", "profit")
    }
    unallocated = rows(
        "SELECT kind,COALESCE(SUM(amount),0) amount FROM manual_costs "
        "WHERE day=? AND product_id IS NULL GROUP BY kind",
        (day,),
    )
    unallocated_map = {item["kind"]: item["amount"] for item in unallocated}
    totals["ads"] = round(totals["ads"] + unallocated_map.get("ads", 0), 2)
    totals["extra_costs"] = round(totals["extra_costs"] + unallocated_map.get("extra", 0), 2)
    totals["profit"] = round(totals["profit"] - sum(unallocated_map.values()), 2)
    totals.update(
        sku_count=len(items),
        drr=round(totals["ads"] / totals["revenue"] * 100, 2) if totals["revenue"] else None,
        margin=round(totals["profit"] / totals["revenue"] * 100, 2) if totals["revenue"] else None,
    )
    return items, totals, {"ads": unallocated_map.get("ads", 0), "extra": unallocated_map.get("extra", 0)}


def daily_report(day: date, search: str = "", scheme: str = "") -> dict:
    clauses, params = ["k.day=?"], [str(day)]
    if search:
        clauses.append("(p.name LIKE ? OR p.sku_original LIKE ? OR p.sku_normalized LIKE ?)")
        params += [f"%{search}%"] * 3
    if scheme in ("FBO", "FBS"):
        clauses.append("p.scheme=?")
        params.append(scheme)
    data = rows(
        "SELECT p.id product_id,p.name,p.sku_original sku,p.scheme,k.sold,k.returns,k.cancelled,"
        "k.revenue,k.ads,k.extra_costs,k.profit,k.status,k.source,"
        "COALESCE((SELECT comment FROM daily_notes n WHERE n.day=k.day AND n.product_id=k.product_id),'') note "
        f"FROM daily_kpi k JOIN products p ON p.id=k.product_id WHERE {' AND '.join(clauses)} "
        "ORDER BY k.profit DESC",
        tuple(params),
    )
    items, totals, unallocated = apply_manual_costs(data, str(day))
    return {"items": items, "totals": totals, "unallocated_costs": unallocated}


def timeseries_report(from_day: date, to_day: date) -> dict:
    items = []
    max_value = 0.0
    current = from_day
    while current <= to_day:
        totals = daily_report(current)["totals"]
        row = {
            "day": current.isoformat(),
            "revenue": totals["revenue"],
            "profit": totals["profit"],
            "ads": totals["ads"],
            "sold": totals["sold"],
        }
        items.append(row)
        max_value = max(max_value, totals["revenue"] or 0, abs(totals["profit"] or 0))
        current += timedelta(days=1)
    return {"items": items, "max_value": max_value}


def _signal(code: str, severity: str, title: str, subtitle: str, count: int, amount: float | None,
            page: str, filters: dict) -> dict:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "subtitle": subtitle,
        "count": count,
        "amount": amount,
        "target": {"page": page, "filters": filters},
    }


def alerts_report(from_day: date, to_day: date) -> dict:
    by_product: dict[int, dict] = {}
    current = from_day
    while current <= to_day:
        for item in daily_report(current)["items"]:
            row = by_product.setdefault(
                item["product_id"],
                {"profit": 0.0, "revenue": 0.0, "ads": 0.0, "sold": 0},
            )
            row["profit"] += item["profit"] or 0
            row["revenue"] += item["revenue"] or 0
            row["ads"] += item["ads"] or 0
            row["sold"] += item["sold"] or 0
        current += timedelta(days=1)

    signals: list[dict] = []
    losses = [row for row in by_product.values() if row["profit"] < 0]
    if losses:
        amount = round(abs(sum(row["profit"] for row in losses)), 2)
        signals.append(_signal(
            "loss_makers", "red",
            f"{_ru_count(len(losses), 'товар работает', 'товара работают', 'товаров работают')} в минус",
            f"Потери {amount:,.2f} ₽".replace(",", " ").replace(".", ","),
            len(losses), amount,
            "Ежедневный контроль", {"kind": "loss"},
        ))

    high_drr = [
        row for row in by_product.values()
        if row["revenue"] and row["ads"] / row["revenue"] > DRR_THRESHOLD
    ]
    if high_drr:
        signals.append(_signal(
            "high_drr", "orange",
            f"ДРР выше {int(DRR_THRESHOLD * 100)}%",
            f"У {_ru_count(len(high_drr), 'товара', 'товаров', 'товаров')}",
            len(high_drr), None,
            "Ежедневный контроль", {"kind": "high_drr"},
        ))

    missing = rows(
        "SELECT p.id FROM products p "
        "LEFT JOIN unit_economics u ON u.id=("
        "SELECT id FROM unit_economics WHERE product_id=p.id ORDER BY valid_from DESC LIMIT 1) "
        "WHERE COALESCE(u.purchase_price,0)=0"
    )
    if missing:
        signals.append(_signal(
            "missing_cost", "yellow",
            "Не заполнена себестоимость",
            f"{_ru_count(len(missing), 'товар требует', 'товара требуют', 'товаров требуют')} настройки",
            len(missing), None,
            "Товары и юнит-экономика", {"kind": "missing_cost"},
        ))

    unlinked = rows("SELECT id FROM products WHERE mapping_status<>'linked'")
    if unlinked:
        signals.append(_signal(
            "unlinked", "blue",
            "Не связано с Ozon",
            f"{_ru_count(len(unlinked), 'SKU не сопоставлен', 'SKU не сопоставлены', 'SKU не сопоставлены')}",
            len(unlinked), None,
            "Товары и юнит-экономика", {"kind": "unlinked"},
        ))

    last = one("SELECT status,finished_at,error FROM sync_runs ORDER BY id DESC LIMIT 1")
    if last and last["status"] == "error":
        signals.append(_signal(
            "sync_failed", "red",
            "Синхронизация завершилась ошибкой",
            (last["error"] or "Повторите синхронизацию")[:120],
            1, None,
            "Главная", {"kind": "sync"},
        ))

    last_success = one(
        "SELECT finished_at FROM sync_runs WHERE status IN ('success','partial') "
        "AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    )
    threshold = stale_after_hours()
    if not last_success:
        signals.append(_signal(
            "sync_stale", "orange",
            "Нет актуальных данных",
            "Подключите кабинет Ozon и запустите синхронизацию",
            1, None,
            "Главная", {"kind": "sync"},
        ))
    elif threshold > 0:
        try:
            finished = datetime.fromisoformat(last_success["finished_at"])
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - finished > timedelta(hours=threshold):
                signals.append(_signal(
                    "sync_stale", "orange",
                    "Данные устарели",
                    f"Последняя успешная синхронизация была более {threshold} ч назад",
                    1, None,
                    "Главная", {"kind": "sync"},
                ))
        except ValueError:
            pass

    signals.sort(key=lambda item: SEVERITY_ORDER.get(item["severity"], 9))
    return {"items": signals, "total": len(signals)}
