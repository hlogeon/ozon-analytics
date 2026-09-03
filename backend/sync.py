from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

from .config import sync_interval_minutes
from .database import connection, json_dump, one
from .ozon import OzonClient
from .skus import normalize_sku

log = logging.getLogger("ozon.sync")

MOSCOW = ZoneInfo("Europe/Moscow")
OVERLAP_DAYS = 3
DEFAULT_WINDOW_DAYS = 14
MAX_WINDOW_DAYS = 90
# Only these tasks cover a contiguous window up to their window_to, so only they
# may advance the incremental watermark. Short "fast" refreshes and one-off
# "manual-range" backfills leave gaps and must never shrink the next full sync.
WATERMARK_TASKS = ("full",)

_sync_lock = threading.Lock()
_scheduler_state: dict[str, object] = {
    "next_run_at": None,
    "interval_minutes": 0,
}


class SyncBusyError(RuntimeError):
    pass


class SyncError(RuntimeError):
    pass


def as_number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def day_in_moscow(value: str | None) -> str:
    if not value:
        return datetime.now(MOSCOW).date().isoformat()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(MOSCOW).date().isoformat()
    except ValueError:
        return value[:10]


def is_settled(posting: dict) -> bool:
    finance = posting.get("financial_data") or {}
    if finance.get("posting_services"):
        return True
    for item in finance.get("products") or []:
        if item.get("item_services"):
            return True
        if as_number(item.get("commission_amount")) or as_number(item.get("payout")):
            return True
    return False


def posting_fees(posting: dict) -> tuple[dict[str, float], float]:
    finance = posting.get("financial_data") or {}
    by_offer: dict[str, float] = {}
    for item in finance.get("products") or []:
        fee = abs(as_number(item.get("commission_amount")))
        fee += sum(abs(as_number(value)) for value in (item.get("item_services") or {}).values())
        identifiers = {item.get("offer_id"), item.get("product_id")}
        for identifier in identifiers - {None, ""}:
            key = normalize_sku(identifier)
            by_offer[key] = by_offer.get(key, 0) + fee
    common = sum(abs(as_number(value)) for value in (finance.get("posting_services") or {}).values())
    return by_offer, common


def upsert_product(db, item: dict, scheme: str | None = None) -> int:
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
    price = as_number(item.get("price"))
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
        (new_id, datetime.now(MOSCOW).date().isoformat()),
    )
    return new_id


def rebuild_live_kpi(db, target_day: str) -> int:
    """Rebuild daily_kpi for exactly one Moscow calendar day.

    Postings are stored with UTC occurred_at, so a Moscow day can straddle
    two UTC calendar dates (e.g. 21:00-24:00 UTC is already the next day in
    Moscow). Scan a day on each side by UTC date and then filter precisely
    by the Moscow-converted day, instead of trusting the UTC date substring
    directly - otherwise postings near midnight get attributed to the wrong
    day and, worse, can collide with the (day, product_id) row already
    rebuilt for their real day by another chunk.
    """
    day_value = date.fromisoformat(target_day)
    scan_from = (day_value - timedelta(days=1)).isoformat()
    scan_to = (day_value + timedelta(days=1)).isoformat()
    aggregates: dict[tuple[str, int], dict] = {}
    postings = db.execute(
        "SELECT scheme,status,occurred_at,raw_json FROM ozon_postings "
        "WHERE substr(occurred_at,1,10) BETWEEN ? AND ?",
        (scan_from, scan_to),
    ).fetchall()
    for stored in postings:
        day = day_in_moscow(stored["occurred_at"])
        if day != target_day:
            continue
        posting = json.loads(stored["raw_json"])
        status = (posting.get("status") or stored["status"] or "").casefold()
        products = posting.get("products") or []
        posting_revenue = sum(
            as_number(item.get("price")) * int(item.get("quantity") or 1) for item in products
        )
        fees_by_offer, common_fees = posting_fees(posting)
        settled = is_settled(posting)
        for item in products:
            product_id = upsert_product(db, item, stored["scheme"])
            quantity = int(item.get("quantity") or 1)
            cancelled = quantity if "cancel" in status else 0
            returned = quantity if "return" in status else 0
            sold = 0 if cancelled or returned else quantity
            revenue = as_number(item.get("price")) * sold
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
    db.execute("DELETE FROM daily_kpi WHERE source='ozon' AND day=?", (target_day,))
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


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def sync_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    moment = now or datetime.now(timezone.utc)
    placeholders = ",".join("?" * len(WATERMARK_TASKS))
    last = one(
        f"SELECT window_to FROM sync_runs WHERE task IN ({placeholders}) "
        "AND status IN ('success','partial') AND window_to IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        WATERMARK_TASKS,
    )
    if not last:
        return moment - timedelta(days=DEFAULT_WINDOW_DAYS), moment
    since = _parse_iso(last["window_to"]) - timedelta(days=OVERLAP_DAYS)
    floor = moment - timedelta(days=MAX_WINDOW_DAYS)
    if since < floor:
        since = floor
    return since, moment


def moscow_day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC (start, end) bounds of one calendar day in Europe/Moscow."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=MOSCOW)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def iter_day_chunks(window_from: datetime, window_to: datetime) -> Iterator[tuple[date, datetime, datetime]]:
    """Split [window_from, window_to) into per-Moscow-day (day, start, end) UTC chunks.

    Chunking keeps each Ozon API page count small (a day is a few pages, not
    thousands), so pagination safety valves in OzonClient never silently
    truncate real data.
    """
    if window_from >= window_to:
        return
    current_day = window_from.astimezone(MOSCOW).date()
    end_day = window_to.astimezone(MOSCOW).date()
    while current_day <= end_day:
        day_start, day_end = moscow_day_bounds(current_day)
        start = max(day_start, window_from)
        end = min(day_end, window_to)
        if start < end:
            yield current_day, start, end
        current_day += timedelta(days=1)


def is_sync_running() -> bool:
    if _sync_lock.locked():
        return True
    last = one("SELECT status FROM sync_runs WHERE status='running' LIMIT 1")
    return bool(last)


def recover_stale_runs() -> None:
    with connection() as db:
        db.execute(
            "UPDATE sync_runs SET status='error',finished_at=CURRENT_TIMESTAMP,"
            "error=? WHERE status='running'",
            ("Процесс синхронизации прерван",),
        )


def begin_sync_run(task: str, window_from: datetime, window_to: datetime) -> int:
    """Synchronously claim the sync lock and register a 'running' sync_runs row.

    Fast (single INSERT) so callers such as the API can report the running
    state immediately, then hand the heavy work (execute_sync_run) to a
    background thread without any race on /api/ozon/status.
    """
    if not _sync_lock.acquire(blocking=False):
        raise SyncBusyError("Синхронизация уже выполняется")
    if one("SELECT id FROM sync_runs WHERE status='running' LIMIT 1"):
        _sync_lock.release()
        raise SyncBusyError("Синхронизация уже выполняется")
    with connection() as db:
        run_id = db.execute(
            "INSERT INTO sync_runs(task,status,window_from,window_to) VALUES(?,'running',?,?)",
            (task, window_from.isoformat(), window_to.isoformat()),
        ).lastrowid
    return run_id


def execute_sync_run(run_id: int, credentials: dict, window_from: datetime, window_to: datetime) -> dict:
    """Do the actual read-only fetch + upsert work for a run started with begin_sync_run.

    Iterates day-by-day (Moscow calendar days) so each Ozon API page count
    stays tiny. The watermark (sync_runs.window_to) only ever advances to the
    end of the last fully-completed day: if a chunk fails partway through,
    already-committed days stay in the database, the run is marked 'partial',
    and the next sync resumes right after the last successful day instead of
    silently losing the gap.
    """
    progressed_to = window_from
    postings_count = products_count = operations_count = kpi_rows_total = 0
    failure: Exception | None = None
    try:
        client = OzonClient(**credentials)
        product_items = list(client.iter_products())
        details = {
            item.get("id") or item.get("product_id"): item
            for item in client.iter_product_info(
                [item.get("product_id") for item in product_items]
            )
        }
        with connection() as db:
            for item in product_items:
                extra = details.get(item.get("product_id")) or {}
                upsert_product(db, {
                    **item,
                    "name": extra.get("name") or item.get("name"),
                    "price": extra.get("price") or extra.get("marketing_price") or item.get("price"),
                })
        products_count = len(product_items)
        for chunk_day, chunk_from, chunk_to in iter_day_chunks(window_from, window_to):
            with connection() as db:
                for scheme in ("FBS", "FBO"):
                    for posting in client.iter_postings(scheme, chunk_from.isoformat(), chunk_to.isoformat()):
                        number = posting.get("posting_number") or posting.get("order_number")
                        if not number:
                            continue
                        for item in posting.get("products") or []:
                            upsert_product(db, item, scheme)
                        db.execute(
                            "INSERT INTO ozon_postings(posting_number,scheme,status,occurred_at,raw_json) "
                            "VALUES(?,?,?,?,?) ON CONFLICT(posting_number) DO UPDATE SET "
                            "status=excluded.status,raw_json=excluded.raw_json,updated_at=CURRENT_TIMESTAMP",
                            (number, scheme, posting.get("status"),
                             posting.get("in_process_at") or posting.get("created_at"), json_dump(posting)),
                        )
                        postings_count += 1
                for operation in client.iter_finance(chunk_from, chunk_to):
                    operation_id = str(operation.get("operation_id") or operation.get("id") or "")
                    if not operation_id:
                        continue
                    db.execute(
                        "INSERT INTO ozon_finance_operations(operation_id,occurred_at,operation_type,amount,raw_json) "
                        "VALUES(?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET occurred_at=excluded.occurred_at,"
                        "operation_type=excluded.operation_type,amount=excluded.amount,raw_json=excluded.raw_json",
                        (operation_id, operation.get("operation_date") or operation.get("created_at"),
                         operation.get("operation_type"), as_number(operation.get("amount")), json_dump(operation)),
                    )
                    operations_count += 1
                kpi_rows_total += rebuild_live_kpi(db, chunk_day.isoformat())
            progressed_to = chunk_to
    except Exception as exc:
        failure = exc
    finally:
        records = postings_count + products_count + operations_count + kpi_rows_total
        if failure is None:
            status = "success"
        elif progressed_to > window_from:
            status = "partial"
        else:
            status = "error"
        with connection() as db:
            db.execute(
                "UPDATE sync_runs SET status=?,finished_at=CURRENT_TIMESTAMP,records=?,window_to=?,error=? "
                "WHERE id=?",
                (status, records, progressed_to.isoformat(), str(failure)[:500] if failure else None, run_id),
            )
        _sync_lock.release()
    if status == "error":
        raise SyncError(str(failure)) from failure
    latest = one("SELECT MAX(day) day FROM daily_kpi WHERE source='ozon'")
    return {"status": status, "records": records, "latest_day": latest["day"] if latest else None}


def run_sync(credentials: dict, since: datetime | None = None, to: datetime | None = None,
             task: str = "full") -> dict:
    """Blocking full sync: claim the run and execute it on the calling thread/task.

    Used by the background scheduler. The API uses begin_sync_run +
    execute_sync_run directly so it can hand the heavy work to a thread and
    respond immediately.
    """
    if not credentials:
        raise SyncError("Сначала подключите кабинет Ozon")
    now = to or datetime.now(timezone.utc)
    window_from, window_to = (since, now) if since is not None else sync_window(now)
    run_id = begin_sync_run(task, window_from, window_to)
    return execute_sync_run(run_id, credentials, window_from, window_to)


def scheduler_status() -> dict:
    return {
        "running": is_sync_running(),
        "interval_minutes": _scheduler_state["interval_minutes"],
        "next_run_at": _scheduler_state["next_run_at"],
        "window": {
            "overlap_days": OVERLAP_DAYS,
            "default_days": DEFAULT_WINDOW_DAYS,
            "max_days": MAX_WINDOW_DAYS,
        },
    }


async def _scheduler_loop(credentials: dict) -> None:
    interval = sync_interval_minutes()
    _scheduler_state["interval_minutes"] = interval
    if interval <= 0:
        _scheduler_state["next_run_at"] = None
        return
    while True:
        next_at = datetime.now(timezone.utc) + timedelta(minutes=interval)
        _scheduler_state["next_run_at"] = next_at.isoformat()
        await asyncio.sleep(interval * 60)
        if not credentials:
            continue
        try:
            await asyncio.to_thread(run_sync, dict(credentials))
        except SyncBusyError:
            log.info("Плановая синхронизация пропущена: уже выполняется")
        except SyncError as exc:
            log.warning("Плановая синхронизация завершилась ошибкой: %s", exc)
        except Exception:
            log.exception("Плановая синхронизация упала")


def start_scheduler(credentials: dict) -> asyncio.Task | None:
    interval = sync_interval_minutes()
    _scheduler_state["interval_minutes"] = interval
    if interval <= 0:
        _scheduler_state["next_run_at"] = None
        return None
    return asyncio.create_task(_scheduler_loop(credentials))


async def stop_scheduler(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    _scheduler_state["next_run_at"] = None
