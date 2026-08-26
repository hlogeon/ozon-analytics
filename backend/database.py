from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path(os.getenv("OZON_ANALYTICS_DB", Path(__file__).parent / "analytics.db"))


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
              id INTEGER PRIMARY KEY, sku_original TEXT NOT NULL UNIQUE,
              sku_normalized TEXT NOT NULL, name TEXT NOT NULL, offer_id TEXT,
              ozon_product_id INTEGER, scheme TEXT, price REAL NOT NULL DEFAULT 0,
              mapping_status TEXT NOT NULL DEFAULT 'unmatched', source TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS unit_economics (
              id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL,
              valid_from TEXT NOT NULL, purchase_price REAL NOT NULL DEFAULT 0,
              marking REAL NOT NULL DEFAULT 0, packaging REAL NOT NULL DEFAULT 0,
              inbound_delivery REAL NOT NULL DEFAULT 0, cross_dock REAL NOT NULL DEFAULT 0,
              tax_rate REAL NOT NULL DEFAULT 0.06, planned_commission REAL NOT NULL DEFAULT 0,
              planned_logistics REAL NOT NULL DEFAULT 0, other_fixed REAL NOT NULL DEFAULT 0,
              FOREIGN KEY(product_id) REFERENCES products(id)
            );
            CREATE TABLE IF NOT EXISTS daily_kpi (
              id INTEGER PRIMARY KEY, day TEXT NOT NULL, product_id INTEGER NOT NULL,
              sold INTEGER NOT NULL DEFAULT 0, returns INTEGER NOT NULL DEFAULT 0,
              cancelled INTEGER NOT NULL DEFAULT 0, revenue REAL NOT NULL DEFAULT 0,
              ozon_fees REAL NOT NULL DEFAULT 0, cogs REAL NOT NULL DEFAULT 0,
              tax REAL NOT NULL DEFAULT 0, ads REAL NOT NULL DEFAULT 0,
              extra_costs REAL NOT NULL DEFAULT 0, profit REAL NOT NULL DEFAULT 0,
              status TEXT NOT NULL, source TEXT NOT NULL, raw_json TEXT,
              UNIQUE(day, product_id), FOREIGN KEY(product_id) REFERENCES products(id)
            );
            CREATE TABLE IF NOT EXISTS manual_costs (
              id INTEGER PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL,
              product_id INTEGER, amount REAL NOT NULL CHECK(amount > 0), comment TEXT,
              author TEXT NOT NULL DEFAULT 'Администратор', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(product_id) REFERENCES products(id)
            );
            CREATE TABLE IF NOT EXISTS ozon_postings (
              posting_number TEXT PRIMARY KEY, scheme TEXT NOT NULL, status TEXT,
              occurred_at TEXT, raw_json TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ozon_finance_operations (
              operation_id TEXT PRIMARY KEY, occurred_at TEXT, operation_type TEXT,
              amount REAL NOT NULL DEFAULT 0, raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stock_snapshots (
              id INTEGER PRIMARY KEY, product_id INTEGER, captured_at TEXT NOT NULL,
              scheme TEXT, warehouse TEXT, available INTEGER NOT NULL DEFAULT 0,
              raw_json TEXT, FOREIGN KEY(product_id) REFERENCES products(id)
            );
            CREATE TABLE IF NOT EXISTS sync_runs (
              id INTEGER PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL,
              started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT,
              records INTEGER NOT NULL DEFAULT 0, error TEXT
            );
            CREATE TABLE IF NOT EXISTS import_runs (
              id INTEGER PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL,
              products INTEGER NOT NULL DEFAULT 0, rows INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, details TEXT
            );
            """
        )


def rows(query: str, params: tuple = ()) -> list[dict]:
    with connection() as db:
        return [dict(row) for row in db.execute(query, params).fetchall()]


def one(query: str, params: tuple = ()) -> dict | None:
    result = rows(query, params)
    return result[0] if result else None


def json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
