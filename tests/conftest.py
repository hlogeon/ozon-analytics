import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
os.environ["OZON_ANALYTICS_DB"] = str(Path(__file__).parent / "test.db")
os.environ["OZON_SELLER_ID"] = ""
os.environ["OZON_CLIENT_ID"] = ""
os.environ["OZON_API_KEY"] = ""
os.environ["OZON_SYNC_INTERVAL_MINUTES"] = "0"

import pytest
from fastapi.testclient import TestClient

from backend.database import db_path, init_db
from backend.main import _credentials, app
from backend.sync import _sync_lock


def pytest_configure(config):
    config.addinivalue_line("markers", "live: read-only live Ozon Seller API")


def pytest_collection_modifyitems(config, items):
    markexpr = config.option.markexpr or ""
    if "live" in markexpr:
        return
    skip_live = pytest.mark.skip(reason="live Ozon API, запустите с -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def fresh_db():
    db_path().unlink(missing_ok=True)
    init_db()
    _credentials.clear()
    if _sync_lock.locked():
        _sync_lock.release()
    yield
    _credentials.clear()
    if _sync_lock.locked():
        _sync_lock.release()
    db_path().unlink(missing_ok=True)


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value
