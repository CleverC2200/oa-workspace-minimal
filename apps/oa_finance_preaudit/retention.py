from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import shutil
from typing import Callable, Iterable


MARKER_NAME = ".oa-retention-root"
MARKER_VALUE = "oa-finance-preaudit-retention-v1\n"
RETENTION_DAYS = 30
_KINDS = {"artifact", "log", "metric", "material", "temp", "task"}
_WRITE_STATES = {"confirmed", "confirmed_no_write", "not_applicable", "unknown"}


@dataclass(frozen=True)
class RetentionRecord:
    key: str
    workflow_key: str
    kind: str
    created_at: datetime
    closed: bool
    write_state: str
    relative_path: str | None = None


@dataclass(frozen=True)
class RetentionAction:
    key: str
    action: str
    reason: str
    relative_path: str | None = None


def _validate_record(record: RetentionRecord, now: datetime) -> None:
    if (
        not record.key
        or not record.workflow_key
        or record.kind not in _KINDS
        or record.write_state not in _WRITE_STATES
        or record.created_at.tzinfo is None
        or now.tzinfo is None
        or record.created_at > now
    ):
        raise ValueError("retention_record_invalid")
    if record.kind != "task" and not record.relative_path:
        raise ValueError("retention_record_invalid")


def plan_retention(
    records: Iterable[RetentionRecord],
    *,
    now: datetime,
    retention_days: int = RETENTION_DAYS,
) -> list[RetentionAction]:
    if isinstance(retention_days, bool) or not isinstance(retention_days, int):
        raise ValueError("retention_days_invalid")
    if retention_days < 1 or now.tzinfo is None:
        raise ValueError("retention_days_invalid")
    items = list(records)
    for item in items:
        _validate_record(item, now)
    keys = [item.key for item in items]
    if len(keys) != len(set(keys)):
        raise ValueError("retention_record_duplicate")

    latest_task_time: dict[str, datetime] = {}
    for item in items:
        if item.kind == "task":
            latest_task_time[item.workflow_key] = max(
                item.created_at,
                latest_task_time.get(item.workflow_key, item.created_at),
            )

    cutoff = now - timedelta(days=retention_days)
    actions: list[RetentionAction] = []
    for item in items:
        if not item.closed:
            action, reason = "keep", "not_closed"
        elif item.write_state == "unknown":
            action, reason = "keep", "write_state_unknown"
        elif (
            item.kind == "task"
            and item.created_at == latest_task_time[item.workflow_key]
        ):
            action, reason = "keep", "latest_task"
        elif item.created_at < cutoff:
            action = "delete_task" if item.kind == "task" else "delete_path"
            reason = "expired"
        elif item.kind == "task":
            action, reason = "archive_task", "previous_task"
        else:
            action, reason = "keep", "within_retention"
        actions.append(
            RetentionAction(
                key=item.key,
                action=action,
                reason=reason,
                relative_path=item.relative_path,
            )
        )
    return actions


def initialize_root(root: Path) -> None:
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("retention_root_invalid")
    marker = root / MARKER_NAME
    if marker.is_symlink():
        raise ValueError("retention_root_invalid")
    if marker.exists():
        if marker.read_text(encoding="utf-8") != MARKER_VALUE:
            raise ValueError("retention_root_invalid")
        return
    try:
        with marker.open("x", encoding="utf-8") as handle:
            handle.write(MARKER_VALUE)
    except FileExistsError as error:
        raise ValueError("retention_root_invalid") from error


def _validated_root(root: Path) -> Path:
    raw = Path(root)
    if raw.is_symlink() or not raw.is_dir():
        raise ValueError("retention_root_invalid")
    resolved = raw.resolve(strict=True)
    marker = resolved / MARKER_NAME
    try:
        marker_value = marker.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError("retention_root_unmarked") from error
    if marker_value != MARKER_VALUE:
        raise ValueError("retention_root_unmarked")
    return resolved


def _validated_target(root: Path, relative_path: str | None) -> Path:
    if not relative_path or Path(relative_path).is_absolute():
        raise ValueError("retention_path_outside_root")
    target = (root / relative_path).resolve(strict=False)
    if target == root or root not in target.parents:
        raise ValueError("retention_path_outside_root")
    return target


def _receipt(*, now: datetime, archived: int = 0, deleted: int = 0,
             missing: int = 0, failed: int = 0) -> dict:
    return {
        "schema_version": 1,
        "started_at": now.isoformat(),
        "completed_at": now.isoformat(),
        "archived": archived,
        "deleted": deleted,
        "missing": missing,
        "failed": failed,
        "status": "ok" if failed == 0 else "partial_failure",
    }


def apply_file_actions(
    root: Path,
    actions: Iterable[RetentionAction],
    *,
    now: datetime,
) -> dict:
    resolved_root = _validated_root(root)
    targets = [
        (action, _validated_target(resolved_root, action.relative_path))
        for action in actions
        if action.action == "delete_path"
    ]
    deleted = 0
    missing = 0
    failed = 0
    for _action, target in targets:
        if not target.exists():
            missing += 1
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            deleted += 1
        except OSError:
            failed += 1
    return _receipt(now=now, deleted=deleted, missing=missing, failed=failed)


def apply_task_actions(
    actions: Iterable[RetentionAction],
    *,
    archive_task: Callable[[str], object],
    delete_task: Callable[[str], object],
    now: datetime,
) -> dict:
    archived = 0
    deleted = 0
    failed = 0
    for action in actions:
        try:
            if action.action == "archive_task":
                archive_task(action.key)
                archived += 1
            elif action.action == "delete_task":
                delete_task(action.key)
                deleted += 1
        except Exception:
            failed += 1
    return _receipt(now=now, archived=archived, deleted=deleted, failed=failed)
