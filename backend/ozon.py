from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator


class OzonError(RuntimeError):
    pass


READ_ONLY_PATHS = frozenset({
    "/v1/seller/info",
    "/v3/product/list",
    "/v3/product/info/list",
    "/v3/posting/fbs/list",
    "/v2/posting/fbo/list",
    "/v3/finance/transaction/list",
})

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 30
DEFAULT_PAGE_SIZE = 1000
# Safety valve against runaway/broken pagination, not a silent data cap: hitting it raises OzonError.
DEFAULT_MAX_PAGES = 200
DEFAULT_PAGE_PAUSE = 0.05
PRODUCT_INFO_BATCH = 1000

Transport = Callable[[str, dict, dict], dict]


def finance_period(value: datetime | str) -> str:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.isoformat(timespec="seconds") + "Z"


def retry_delay(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), MAX_RETRY_WAIT)
        except ValueError:
            pass
    return min(2 ** attempt, MAX_RETRY_WAIT)


def http_transport(url: str, headers: dict, payload: dict) -> dict:
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            if exc.code in (401, 403):
                raise OzonError("Ошибка авторизации или недостаточно прав") from exc
            if exc.code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS - 1:
                header = exc.headers.get("Retry-After") if exc.headers else None
                time.sleep(retry_delay(attempt, header))
                last_error = exc
                continue
            raise OzonError(f"Ozon API вернул HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(retry_delay(attempt))
                last_error = exc
                continue
            raise OzonError("Ozon API временно недоступен") from exc
    raise OzonError("Ozon API временно недоступен") from last_error


def _result_items(payload: dict, key: str) -> list:
    result = payload.get("result")
    if isinstance(result, dict):
        return list(result.get(key) or [])
    if isinstance(result, list) and key == "postings":
        return result
    return list(payload.get(key) or [])


@dataclass
class OzonClient:
    client_id: str
    api_key: str
    transport: Transport = http_transport
    base_url: str = "https://api-seller.ozon.ru"
    page_pause: float = DEFAULT_PAGE_PAUSE
    max_pages: int = DEFAULT_MAX_PAGES

    @property
    def headers(self) -> dict:
        return {"Client-Id": self.client_id, "Api-Key": self.api_key, "Content-Type": "application/json"}

    def post(self, path: str, payload: dict) -> dict:
        if path not in READ_ONLY_PATHS:
            raise OzonError(f"Запрещён не-read-only вызов Ozon API: {path}")
        return self.transport(self.base_url + path, self.headers, payload)

    def check(self) -> dict:
        return self.post("/v1/seller/info", {})

    def products(self, last_id: str = "", limit: int = DEFAULT_PAGE_SIZE) -> dict:
        return self.post(
            "/v3/product/list",
            {"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": limit},
        )

    def product_info(self, product_ids: list | None = None, offer_ids: list[str] | None = None) -> dict:
        return self.post(
            "/v3/product/info/list",
            {"product_id": list(product_ids or []), "offer_id": list(offer_ids or []), "sku": []},
        )

    def fbs_postings(self, since: str, to: str, offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> dict:
        return self.post(
            "/v3/posting/fbs/list",
            {
                "dir": "ASC",
                "filter": {"since": since, "to": to},
                "limit": limit,
                "offset": offset,
                "with": {"analytics_data": True, "financial_data": True},
            },
        )

    def fbo_postings(self, since: str, to: str, offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> dict:
        return self.post(
            "/v2/posting/fbo/list",
            {
                "dir": "ASC",
                "filter": {"since": since, "to": to},
                "limit": limit,
                "offset": offset,
                "translit": False,
                "with": {"analytics_data": True, "financial_data": True},
            },
        )

    def finance(self, since: datetime | str, to: datetime | str, page: int = 1,
                page_size: int = DEFAULT_PAGE_SIZE) -> dict:
        return self.post(
            "/v3/finance/transaction/list",
            {
                "filter": {
                    "date": {"from": finance_period(since), "to": finance_period(to)},
                    "operation_type": [],
                    "posting_number": "",
                    "transaction_type": "all",
                },
                "page": page,
                "page_size": page_size,
            },
        )

    def _pause(self, more: bool) -> None:
        if more and self.page_pause > 0:
            time.sleep(self.page_pause)

    def iter_products(self, limit: int = DEFAULT_PAGE_SIZE,
                      max_pages: int | None = None) -> Iterator[dict]:
        last_id = ""
        pages = max_pages if max_pages is not None else self.max_pages
        for _ in range(pages):
            payload = self.products(last_id=last_id, limit=limit)
            items = _result_items(payload, "items")
            yield from items
            result = payload.get("result") or {}
            last_id = result.get("last_id") or "" if isinstance(result, dict) else ""
            if len(items) < limit or not last_id:
                return
            self._pause(True)
        raise OzonError(f"Превышен лимит страниц ({pages}) при получении товаров: увеличьте max_pages")

    def iter_product_info(self, product_ids: list) -> Iterator[dict]:
        identifiers = [item for item in product_ids if item]
        for start in range(0, len(identifiers), PRODUCT_INFO_BATCH):
            batch = identifiers[start:start + PRODUCT_INFO_BATCH]
            payload = self.product_info(product_ids=batch)
            items = payload.get("items") or _result_items(payload, "items")
            yield from items
            self._pause(start + PRODUCT_INFO_BATCH < len(identifiers))

    def iter_postings(self, scheme: str, since: str, to: str, limit: int = DEFAULT_PAGE_SIZE,
                      max_pages: int | None = None) -> Iterator[dict]:
        fetch = self.fbs_postings if scheme == "FBS" else self.fbo_postings
        pages = max_pages if max_pages is not None else self.max_pages
        offset = 0
        for _ in range(pages):
            payload = fetch(since, to, offset=offset, limit=limit)
            postings = _result_items(payload, "postings")
            result = payload.get("result")
            has_next = result.get("has_next") if isinstance(result, dict) else None
            yield from postings
            if has_next is False or len(postings) < limit:
                return
            offset += limit
            self._pause(True)
        raise OzonError(
            f"Превышен лимит страниц ({pages}) при получении отправлений {scheme}: сузьте окно или увеличьте max_pages"
        )

    def iter_finance(self, since: datetime | str, to: datetime | str,
                     page_size: int = DEFAULT_PAGE_SIZE,
                     max_pages: int | None = None) -> Iterator[dict]:
        pages = max_pages if max_pages is not None else self.max_pages
        page = 1
        while page <= pages:
            payload = self.finance(since, to, page=page, page_size=page_size)
            result = payload.get("result") or {}
            operations = result.get("operations") or payload.get("operations") or []
            yield from operations
            page_count = result.get("page_count") if isinstance(result, dict) else None
            if not operations:
                return
            if page_count is not None and page >= page_count:
                return
            if len(operations) < page_size:
                return
            page += 1
            self._pause(True)
        raise OzonError(
            f"Превышен лимит страниц ({pages}) при получении финопераций: сузьте окно или увеличьте max_pages"
        )
