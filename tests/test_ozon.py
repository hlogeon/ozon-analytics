import io
import urllib.error
from datetime import datetime, timezone
from email.message import Message
import pytest

from backend.config import ozon_credentials, read_env_file
from backend.main import _seller_name
from backend.sync import is_settled as _is_settled, posting_fees as _posting_fees
from backend.ozon import OzonClient, OzonError, finance_period, http_transport, retry_delay


def test_allowlist_blocks_write_path():
    client = OzonClient("1", "key", transport=lambda *_: {})
    with pytest.raises(OzonError, match="не-read-only"):
        client.post("/v1/product/import", {"items": []})


def test_allowlist_allows_read_paths():
    seen = []

    def transport(url, _headers, payload):
        seen.append(url)
        return {"ok": True, "payload": payload}

    client = OzonClient("1", "key", transport=transport)
    assert client.check()["ok"] is True
    assert seen[0].endswith("/v1/seller/info")


def test_env_file_is_used_when_environ_empty(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OZON_SELLER_ID=seller-1\nOZON_API_KEY=secret-key\n", encoding="utf-8")
    creds = ozon_credentials(env_file, environ={})
    assert creds == {"client_id": "seller-1", "api_key": "secret-key"}


def test_environ_overrides_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OZON_SELLER_ID=from-file\nOZON_API_KEY=file-key\n", encoding="utf-8")
    creds = ozon_credentials(env_file, environ={"OZON_SELLER_ID": "from-env", "OZON_API_KEY": "env-key"})
    assert creds == {"client_id": "from-env", "api_key": "env-key"}


def test_empty_environ_wins_over_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OZON_SELLER_ID=from-file\nOZON_API_KEY=file-key\n", encoding="utf-8")
    creds = ozon_credentials(env_file, environ={"OZON_SELLER_ID": "", "OZON_API_KEY": ""})
    assert creds == {}


def test_read_env_file_ignores_comments(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nOZON_API_KEY='quoted'\n\n", encoding="utf-8")
    assert read_env_file(env_file)["OZON_API_KEY"] == "quoted"


def test_finance_period_uses_protobuf_utc():
    aware = datetime(2026, 9, 3, 10, 15, 30, tzinfo=timezone.utc)
    value = finance_period(aware)
    assert value == "2026-09-03T10:15:30Z"
    assert "+" not in value


def test_finance_request_uses_zulu_dates():
    calls = []

    def transport(_url, _headers, payload):
        calls.append(payload)
        return {"result": {"operations": []}}

    client = OzonClient("1", "key", transport=transport, page_pause=0)
    since = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    to = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    client.finance(since, to)
    period = calls[0]["filter"]["date"]
    assert period["from"] == "2026-09-01T12:00:00Z"
    assert period["to"] == "2026-09-03T12:00:00Z"


def test_iter_postings_accepts_fbo_list_result():
    def transport(_url, _headers, payload):
        return {"result": [{"posting_number": "fbo-1"}]}

    client = OzonClient("1", "key", transport=transport, page_pause=0)
    items = list(client.iter_postings("FBO", "2026-09-01T00:00:00Z", "2026-09-03T00:00:00Z", limit=10))
    assert [item["posting_number"] for item in items] == ["fbo-1"]


def test_iter_postings_stops_when_has_next_is_false():
    calls = []

    def transport(_url, _headers, payload):
        calls.append(payload["offset"])
        return {"result": {"has_next": False, "postings": [{"posting_number": "a"}, {"posting_number": "b"}]}}

    client = OzonClient("1", "key", transport=transport, page_pause=0)
    items = list(client.iter_postings("FBS", "2026-09-01T00:00:00Z", "2026-09-03T00:00:00Z", limit=2))
    assert len(items) == 2
    assert calls == [0]


def test_iter_postings_collects_pages():
    calls = []

    def transport(url, _headers, payload):
        calls.append(payload)
        offset = payload["offset"]
        if offset == 0:
            return {"result": {"postings": [{"posting_number": "a"}, {"posting_number": "b"}]}}
        return {"result": {"postings": [{"posting_number": "c"}]}}

    client = OzonClient("1", "key", transport=transport, page_pause=0)
    items = list(client.iter_postings("FBS", "2026-09-01T00:00:00", "2026-09-03T00:00:00", limit=2))
    assert [item["posting_number"] for item in items] == ["a", "b", "c"]
    assert [call["offset"] for call in calls] == [0, 2]


def test_iter_finance_stops_on_page_count():
    calls = []

    def transport(_url, _headers, payload):
        calls.append(payload["page"])
        return {"result": {"page_count": 2, "operations": [{"operation_id": payload["page"]}]}}

    client = OzonClient("1", "key", transport=transport, page_pause=0)
    items = list(client.iter_finance("2026-09-01T00:00:00", "2026-09-03T00:00:00", page_size=1))
    assert [item["operation_id"] for item in items] == [1, 2]
    assert calls == [1, 2]


def test_iter_products_stops_without_last_id():
    def transport(_url, _headers, payload):
        return {"result": {"items": [{"offer_id": "one"}], "last_id": ""}}

    client = OzonClient("1", "key", transport=transport, page_pause=0)
    items = list(client.iter_products(limit=1, max_pages=5))
    assert len(items) == 1


def test_iter_postings_raises_instead_of_silently_truncating():
    def transport(_url, _headers, payload):
        return {"result": {"has_next": True, "postings": [{"posting_number": "a"}, {"posting_number": "b"}]}}

    client = OzonClient("1", "key", transport=transport, page_pause=0)
    with pytest.raises(OzonError, match="Превышен лимит страниц"):
        list(client.iter_postings("FBS", "2026-09-01T00:00:00Z", "2026-09-03T00:00:00Z", limit=2, max_pages=2))


def test_iter_finance_raises_instead_of_silently_truncating():
    def transport(_url, _headers, payload):
        return {"result": {"operations": [{"operation_id": payload["page"]}]}}

    client = OzonClient("1", "key", transport=transport, page_pause=0)
    with pytest.raises(OzonError, match="Превышен лимит страниц"):
        list(client.iter_finance("2026-09-01T00:00:00", "2026-09-03T00:00:00", page_size=1, max_pages=2))


def test_iter_products_raises_instead_of_silently_truncating():
    def transport(_url, _headers, payload):
        return {"result": {"items": [{"offer_id": "one"}], "last_id": "next"}}

    client = OzonClient("1", "key", transport=transport, page_pause=0)
    with pytest.raises(OzonError, match="Превышен лимит страниц"):
        list(client.iter_products(limit=1, max_pages=2))


def test_retry_delay_honours_retry_after():
    assert retry_delay(0, "4") == 4
    assert retry_delay(0, None) == 1


def test_http_transport_retries_429(monkeypatch):
    sleeps = []
    attempts = {"n": 0}

    def fake_sleep(seconds):
        sleeps.append(seconds)

    class FakeResponse:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(_request, timeout=30):
        attempts["n"] += 1
        if attempts["n"] == 1:
            headers = Message()
            headers["Retry-After"] = "2"
            raise urllib.error.HTTPError(
                "https://api-seller.ozon.ru/v1/seller/info",
                429,
                "Too Many",
                headers,
                io.BytesIO(b"slow down"),
            )
        return FakeResponse()

    monkeypatch.setattr("backend.ozon.time.sleep", fake_sleep)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    payload = http_transport("https://api-seller.ozon.ru/v1/seller/info", {}, {})
    assert payload == {"ok": True}
    assert sleeps == [2]


def test_http_transport_does_not_retry_400(monkeypatch):
    def fake_urlopen(_request, timeout=30):
        raise urllib.error.HTTPError(
            "https://api-seller.ozon.ru/v1/seller/info",
            400,
            "Bad",
            Message(),
            io.BytesIO(b"bad request"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(OzonError, match="HTTP 400"):
        http_transport("https://api-seller.ozon.ru/v1/seller/info", {}, {})


def test_seller_name_from_nested_company():
    assert _seller_name({"company": {"name": "Empire Socks", "legal_name": "ИП"}}) == "Empire Socks"


def test_fbo_financial_data_without_payout_is_preliminary():
    posting = {
        "status": "delivering",
        "products": [{"offer_id": "O5-1", "sku": 2706764236, "price": "296.00", "quantity": 1}],
        "financial_data": {
            "products": [{
                "commission_amount": 0, "payout": 0, "product_id": 2706764236, "price": 296,
            }],
        },
    }
    assert _is_settled(posting) is False
    by_offer, common = _posting_fees(posting)
    assert common == 0
    assert by_offer["2706764236"] == 0


def test_settled_when_commission_present():
    posting = {"financial_data": {"products": [{"offer_id": "live-1", "commission_amount": -100}]}}
    assert _is_settled(posting) is True


@pytest.mark.live
def test_live_seller_info_is_read_only():
    creds = ozon_credentials(environ={})
    if not creds:
        pytest.skip("нет ключей в .env")
    info = OzonClient(**creds, page_pause=0.2).check()
    assert isinstance(info, dict)
    assert info
