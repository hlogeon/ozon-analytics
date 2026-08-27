from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable


class OzonError(RuntimeError):
    pass


Transport = Callable[[str, dict, dict], dict]


def http_transport(url: str, headers: dict, payload: dict) -> dict:
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
        raise OzonError(f"Ozon API вернул HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OzonError("Ozon API временно недоступен") from exc


@dataclass
class OzonClient:
    client_id: str
    api_key: str
    transport: Transport = http_transport
    base_url: str = "https://api-seller.ozon.ru"

    @property
    def headers(self) -> dict:
        return {"Client-Id": self.client_id, "Api-Key": self.api_key, "Content-Type": "application/json"}

    def post(self, path: str, payload: dict) -> dict:
        return self.transport(self.base_url + path, self.headers, payload)

    def check(self) -> dict:
        return self.post("/v1/seller/info", {})

    def products(self, last_id: str = "", limit: int = 1000) -> dict:
        return self.post("/v3/product/list", {"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": limit})

    def fbs_postings(self, since: str, to: str, offset: int = 0) -> dict:
        return self.post("/v3/posting/fbs/list", {"dir": "ASC", "filter": {"since": since, "to": to}, "limit": 1000, "offset": offset, "with": {"analytics_data": True, "financial_data": True}})

    def fbo_postings(self, since: str, to: str, offset: int = 0) -> dict:
        return self.post("/v2/posting/fbo/list", {"dir": "ASC", "filter": {"since": since, "to": to}, "limit": 1000, "offset": offset, "translit": False, "with": {"analytics_data": True, "financial_data": True}})

    def finance(self, since: str, to: str, page: int = 1) -> dict:
        return self.post("/v3/finance/transaction/list", {"filter": {"date": {"from": since, "to": to}, "operation_type": [], "posting_number": "", "transaction_type": "all"}, "page": page, "page_size": 1000})
