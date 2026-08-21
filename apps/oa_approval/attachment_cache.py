from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any


DISABLE_VALUES = {"0", "false", "off", "no"}
DEFAULT_DB_PATH = Path("runtime") / "finance_preaudit_cache.sqlite3"


def is_enabled() -> bool:
    raw = str(os.environ.get("OA_FINANCE_PREAUDIT_CACHE_DB") or "").strip()
    return raw.lower() not in DISABLE_VALUES


def db_path() -> Path:
    raw = str(os.environ.get("OA_FINANCE_PREAUDIT_CACHE_DB") or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_DB_PATH


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.execute("pragma journal_mode=WAL")
    connection.execute(
        """
        create table if not exists attachment_files (
            request_id text not null,
            flow_key text not null,
            resource_key text not null,
            entry_json text not null,
            updated_at text not null,
            primary key (request_id, flow_key, resource_key)
        )
        """
    )
    return connection


def record_attachment_file(
    *,
    request_id: str,
    flow_key: str,
    resource_key: str,
    entry_json: dict[str, Any],
) -> dict[str, Any]:
    if not is_enabled():
        return {"enabled": False, "ok": True, "path": "", "error": ""}
    path = db_path()
    try:
        with _connect(path) as connection:
            connection.execute(
                """
                insert or replace into attachment_files (
                    request_id, flow_key, resource_key, entry_json, updated_at
                ) values (?, ?, ?, ?, ?)
                """,
                (
                    str(request_id),
                    str(flow_key),
                    str(resource_key),
                    json.dumps(entry_json or {}, ensure_ascii=False, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
        return {"enabled": True, "ok": True, "path": str(path), "error": ""}
    except Exception as error:
        return {
            "enabled": True,
            "ok": False,
            "path": str(path),
            "error": str(error),
        }


def latest_attachment_file(
    request_id: str,
    flow_key: str,
    resource_key: str,
) -> dict[str, Any] | None:
    if not is_enabled() or not db_path().exists():
        return None
    try:
        with _connect(db_path()) as connection:
            row = connection.execute(
                """
                select entry_json
                from attachment_files
                where request_id = ? and flow_key = ? and resource_key = ?
                limit 1
                """,
                (str(request_id), str(flow_key), str(resource_key)),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        payload = json.loads(str(row[0] or "{}"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None
