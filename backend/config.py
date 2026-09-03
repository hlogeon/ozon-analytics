from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = ROOT / ".env"


def read_env_file(path: Path | None = None) -> dict[str, str]:
    env_path = path or DEFAULT_ENV
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def load_dotenv(path: Path | None = None, environ: dict[str, str] | None = None) -> dict[str, str]:
    target = os.environ if environ is None else environ
    values = read_env_file(path)
    for key, value in values.items():
        if key not in target:
            target[key] = value
    return values


def ozon_credentials(path: Path | None = None,
                     environ: Mapping[str, str] | None = None) -> dict[str, str]:
    file_values = read_env_file(path)
    if environ is None:
        load_dotenv(path)
        environ = os.environ
    source = dict(file_values)
    for key in ("OZON_SELLER_ID", "OZON_CLIENT_ID", "OZON_API_KEY"):
        if key in environ:
            source[key] = environ[key]
    client_id = (source.get("OZON_SELLER_ID") or source.get("OZON_CLIENT_ID") or "").strip()
    api_key = (source.get("OZON_API_KEY") or "").strip()
    if not client_id or not api_key:
        return {}
    return {"client_id": client_id, "api_key": api_key}


def sync_interval_minutes(environ: Mapping[str, str] | None = None) -> int:
    source = environ if environ is not None else os.environ
    raw = (source.get("OZON_SYNC_INTERVAL_MINUTES") or "30").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 30


def stale_after_hours(environ: Mapping[str, str] | None = None) -> int:
    """Hours after the last successful sync before data is flagged stale (0 disables the check)."""
    source = environ if environ is not None else os.environ
    raw = (source.get("OZON_STALE_AFTER_HOURS") or "6").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 6
