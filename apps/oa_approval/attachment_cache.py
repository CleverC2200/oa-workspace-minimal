from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any

from apps.oa_approval import runtime_control


DISABLE_VALUES = {"0", "false", "off", "no"}
DEFAULT_DB_PATH = Path("runtime") / "finance_preaudit_cache.sqlite3"
CACHE_RETENTION_DAYS = 30
ATTACHMENT_FILES_SCHEMA = (
    ("request_id", "TEXT", 1, None, 1),
    ("flow_key", "TEXT", 1, None, 2),
    ("resource_key", "TEXT", 1, None, 3),
    ("entry_json", "TEXT", 1, None, 0),
    ("updated_at", "TEXT", 1, None, 0),
)


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


def _connect_existing(path: Path) -> sqlite3.Connection:
    target = Path(os.path.abspath(path.expanduser()))
    with runtime_control.pinned_directory(target.parent) as parent_descriptor:
        before = os.stat(
            target.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError("cache_file_unsafe")
        uri = f"{target.as_uri()}?mode=rw&nofollow=1"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            after = os.stat(
                target.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            current = os.stat(target, follow_symlinks=False)
            if (
                not stat.S_ISREG(after.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (before.st_dev, before.st_ino)
                != (after.st_dev, after.st_ino)
                or (before.st_dev, before.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                raise RuntimeError("cache_file_unsafe")
            columns = connection.execute(
                "pragma table_info(attachment_files)"
            ).fetchall()
            schema = tuple(
                (
                    str(column[1]),
                    str(column[2]).upper(),
                    int(column[3]),
                    column[4],
                    int(column[5]),
                )
                for column in columns
            )
            if schema != ATTACHMENT_FILES_SCHEMA:
                raise RuntimeError("cache_file_unsafe")
        except Exception:
            connection.close()
            raise
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


def clean_all_cache() -> dict[str, Any]:
    if not is_enabled():
        return {
            "schema_version": 1,
            "kind": "preaudit_cache_clean_receipt",
            "status": "ok",
            "deleted_entries": 0,
        }
    path = db_path()
    if not path.exists():
        return {
            "schema_version": 1,
            "kind": "preaudit_cache_clean_receipt",
            "status": "ok",
            "deleted_entries": 0,
        }
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("cache_clean_failed")
    try:
        connection = _connect_existing(path)
        try:
            cursor = connection.execute("delete from attachment_files")
            deleted = max(0, int(cursor.rowcount))
            connection.commit()
            connection.execute("pragma wal_checkpoint(truncate)")
            connection.execute("vacuum")
        finally:
            connection.close()
    except Exception as error:
        raise RuntimeError("cache_clean_failed") from error
    return {
        "schema_version": 1,
        "kind": "preaudit_cache_clean_receipt",
        "status": "ok",
        "deleted_entries": deleted,
    }


def prune_expired(
    *,
    now: datetime | None = None,
    retention_days: int = CACHE_RETENTION_DAYS,
) -> dict[str, Any]:
    receipt = {
        "schema_version": 1,
        "kind": "preaudit_cache_maintenance_receipt",
        "status": "ok",
        "deleted_entries": 0,
    }
    if not is_enabled():
        return receipt
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or retention_days != CACHE_RETENTION_DAYS
    ):
        return {**receipt, "status": "failed"}
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        return {**receipt, "status": "failed"}
    path = db_path()
    if not path.exists():
        return receipt
    if path.is_symlink() or not path.is_file():
        return {**receipt, "status": "failed"}
    cutoff = current_time.astimezone(timezone.utc) - timedelta(
        days=retention_days
    )
    try:
        connection = _connect_existing(path)
        try:
            rows = connection.execute(
                "select rowid, updated_at from attachment_files"
            ).fetchall()
            expired: list[int] = []
            for rowid, raw_updated_at in rows:
                try:
                    updated_at = datetime.fromisoformat(str(raw_updated_at))
                except (TypeError, ValueError):
                    expired.append(int(rowid))
                    continue
                if (
                    updated_at.tzinfo is None
                    or updated_at.astimezone(timezone.utc) < cutoff
                ):
                    expired.append(int(rowid))
            if expired:
                connection.executemany(
                    "delete from attachment_files where rowid = ?",
                    ((rowid,) for rowid in expired),
                )
            connection.commit()
            connection.execute("pragma wal_checkpoint(truncate)")
        finally:
            connection.close()
    except Exception:
        return {**receipt, "status": "failed"}
    return {**receipt, "deleted_entries": len(expired)}
