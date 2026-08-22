from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import fcntl
import os
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
    if marker.is_symlink():
        raise ValueError("retention_root_unmarked")
    try:
        marker_value = marker.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError("retention_root_unmarked") from error
    if marker_value != MARKER_VALUE:
        raise ValueError("retention_root_unmarked")
    return resolved


def _validated_target(root: Path, relative_path: str | None) -> Path:
    relative = Path(str(relative_path or ""))
    if (
        not relative_path
        or relative.is_absolute()
        or ".." in relative.parts
        or "." in relative.parts
    ):
        raise ValueError("retention_path_outside_root")
    raw_target = root / relative
    current = raw_target
    while current != root:
        if current.is_symlink():
            raise ValueError("retention_path_outside_root")
        current = current.parent
    target = raw_target.resolve(strict=False)
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


def validate_file_actions(
    root: Path,
    actions: Iterable[RetentionAction],
) -> None:
    resolved_root = _validated_root(root)
    for action in actions:
        if action.action not in {"archive_path", "delete_path", "keep"}:
            raise ValueError("retention_action_invalid")
        _validated_target(resolved_root, action.relative_path)
        if action.action == "archive_path":
            source = _validated_target(resolved_root, action.relative_path)
            destination = _validated_target(
                resolved_root,
                str(Path(".archive") / str(action.relative_path)),
            )
            if source.exists() and destination.exists():
                raise ValueError("retention_archive_conflict")


@contextmanager
def maintenance_lock(root: Path):
    resolved_root = _validated_root(root)
    lock_path = resolved_root / ".retention.lock"
    if lock_path.is_symlink():
        raise ValueError("retention_root_invalid")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ValueError("retention_root_invalid") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield resolved_root
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def apply_file_actions(
    root: Path,
    actions: Iterable[RetentionAction],
    *,
    now: datetime,
) -> dict:
    items = list(actions)
    with maintenance_lock(root) as resolved_root:
        return apply_file_actions_locked(
            resolved_root,
            items,
            now=now,
        )


def apply_file_actions_locked(
    root: Path,
    actions: Iterable[RetentionAction],
    *,
    now: datetime,
) -> dict:
    items = list(actions)
    validate_file_actions(root, items)
    resolved_root = _validated_root(root)
    targets = [
        (action, _validated_target(resolved_root, action.relative_path))
        for action in items
        if action.action in {"archive_path", "delete_path"}
    ]
    archive_targets = {
        action.key: _validated_target(
            resolved_root,
            str(Path(".archive") / str(action.relative_path)),
        )
        for action, _target in targets
        if action.action == "archive_path"
    }
    archived = 0
    deleted = 0
    missing = 0
    failed = 0
    for _action, planned_target in targets:
        target = _validated_target(resolved_root, _action.relative_path)
        if target != planned_target:
            raise ValueError("retention_path_outside_root")
        if not target.exists():
            missing += 1
            continue
        try:
            if _action.action == "archive_path":
                destination = archive_targets[_action.key]
                if destination.exists():
                    raise ValueError("retention_archive_conflict")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination = _validated_target(
                    resolved_root,
                    str(destination.relative_to(resolved_root)),
                )
                target.rename(destination)
                archived += 1
            elif target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            if _action.action == "delete_path":
                deleted += 1
        except OSError:
            failed += 1
    return _receipt(
        now=now,
        archived=archived,
        deleted=deleted,
        missing=missing,
        failed=failed,
    )


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
