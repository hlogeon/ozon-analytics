"""Read-only probe of Ozon Seller API. Does not write to the database."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ozon_credentials
from .ozon import OzonClient

OUTPUT = Path(__file__).resolve().parents[1] / "tmp" / "ozon-probe"


def _dump(name: str, payload: object) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{name}: keys={sorted(payload) if isinstance(payload, dict) else type(payload).__name__}")


def _count(payload: dict, *path: str) -> int:
    current = payload
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return 0
    if isinstance(current, list):
        return len(current)
    return 0


def main() -> int:
    creds = ozon_credentials(environ={})
    if not creds:
        print("Нет OZON_SELLER_ID/OZON_API_KEY в .env")
        return 1
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=2)
    client = OzonClient(**creds, page_pause=0.2)
    print("client_id задан, api_key скрыт")

    info = client.check()
    _dump("seller-info.json", info)
    company = info.get("company") if isinstance(info.get("company"), dict) else {}
    print("company:", company.get("name") or info.get("name"))

    products = client.products(limit=10)
    _dump("products.json", products)
    product_items = (products.get("result") or {}).get("items") or products.get("items") or []
    print("products:", len(product_items))
    product_ids = [item.get("product_id") for item in product_items if item.get("product_id")]
    if product_ids:
        details = client.product_info(product_ids=product_ids[:10])
        _dump("product-info.json", details)
        detail_items = details.get("items") or (details.get("result") or {}).get("items") or []
        print("product info:", len(detail_items), "keys", sorted(detail_items[0]) if detail_items else None)

    fbs = client.fbs_postings(since.isoformat(), now.isoformat(), limit=10)
    _dump("fbs-postings.json", fbs)
    print("fbs postings:", _count(fbs, "result", "postings") or _count(fbs, "result") or _count(fbs, "postings"))

    fbo = client.fbo_postings(since.isoformat(), now.isoformat(), limit=10)
    _dump("fbo-postings.json", fbo)
    print("fbo postings:", _count(fbo, "result", "postings") or _count(fbo, "result") or _count(fbo, "postings"))

    finance = client.finance(since, now, page_size=10)
    _dump("finance.json", finance)
    print("finance operations:", _count(finance, "result", "operations") or _count(finance, "operations"))
    print("probe ok, ответы в", OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
