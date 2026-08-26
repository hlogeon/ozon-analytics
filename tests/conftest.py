import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
os.environ["OZON_ANALYTICS_DB"] = str(Path(__file__).parent / "test.db")

import pytest
from fastapi.testclient import TestClient
from backend.database import DB_PATH
from backend.main import app
from backend.seed import seed_demo


@pytest.fixture(autouse=True)
def fresh_db():
    DB_PATH.unlink(missing_ok=True)
    seed_demo()
    yield
    DB_PATH.unlink(missing_ok=True)


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value
