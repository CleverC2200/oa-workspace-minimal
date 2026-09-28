"""Own and close the existing OA repair reservation; never start tasks or touch OA."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import uuid

from apps import json_artifact

DEFAULT_ROOT = Path.home() / ".codex" / "automations" / "oa-skill" / "repair-dispatch"


@contextmanager
def _lease(root: Path):
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("repair_dispatch_root_invalid")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root / ".dispatch.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _read(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("repair_dispatch_owner_invalid")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("repair_dispatch_owner_invalid")
    return value


def _window(root: Path, key: str) -> Path:
    if not isinstance(key, str) or not key or len(key) > 240:
        raise ValueError("repair_dispatch_window_invalid")
    return root / (hashlib.sha256(key.encode()).hexdigest() + ".json")


def status(root: Path = DEFAULT_ROOT) -> dict:
    active = root / "active"
    if not os.path.lexists(active):
        return {"status": "idle"}
    if active.is_symlink() or not active.is_dir():
        return {"status": "ownership_unverified", "next_step": "verify_ownership"}
    try:
        owner = _read(active / "owner.json")
        record = _read(_window(root, owner["window_key"]))
        if (owner != record or owner.get("schema_version") != 1
                or owner.get("kind") != "oa_repair_dispatch"
                or owner.get("status") not in {"reserved", "running"}
                or not isinstance(owner.get("reservation_id"), str)
                or len(owner["reservation_id"]) != 32):
            raise ValueError("repair_dispatch_owner_invalid")
        created = datetime.fromisoformat(owner["updated_at"])
        if created.tzinfo is None:
            raise ValueError("repair_dispatch_owner_invalid")
        age = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
        return {**owner, "age_seconds": age, "overdue": age > 7200,
                "next_step": "check_task_status" if owner.get("thread_id") else "verify_dispatch_result"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "ownership_unverified", "next_step": "verify_ownership"}


def reserve(root: Path, window_key: str, source_thread_id: str) -> dict:
    with _lease(root):
        target = _window(root, window_key)
        if os.path.lexists(target):
            return {"status": "already_dispatched", "record": _read(target)}
        if status(root)["status"] != "idle":
            return {"status": "waiting_for_user", "reason": "repair_dispatch_active_exists",
                    "active": status(root)}
        (root / "active").mkdir(mode=0o700)
        owner = {"schema_version": 1, "kind": "oa_repair_dispatch", "status": "reserved",
                 "window_key": window_key, "source_thread_id": source_thread_id,
                 "reservation_id": uuid.uuid4().hex, "thread_id": None,
                 "updated_at": datetime.now(timezone.utc).isoformat()}
        json_artifact.write_json_atomic(target, owner, indent=2)
        json_artifact.write_json_atomic(root / "active" / "owner.json", owner, indent=2)
        return owner


def bind(root: Path, reservation_id: str, thread_id: str) -> dict:
    with _lease(root):
        owner = status(root)
        if (owner.get("reservation_id") != reservation_id or owner.get("status") != "reserved"
                or not thread_id):
            raise ValueError("repair_dispatch_owner_invalid")
        owner = _read(root / "active" / "owner.json")
        owner.update(status="running", thread_id=thread_id, updated_at=datetime.now(timezone.utc).isoformat())
        json_artifact.write_json_atomic(_window(root, owner["window_key"]), owner, indent=2)
        json_artifact.write_json_atomic(root / "active" / "owner.json", owner, indent=2)
        return owner


def progress(root: Path, reservation_id: str, thread_id: str) -> dict:
    with _lease(root):
        owner = status(root)
        if (owner.get("reservation_id") != reservation_id or owner.get("thread_id") != thread_id
                or owner.get("status") != "running"):
            raise ValueError("repair_dispatch_owner_invalid")
        record = _read(root / "active" / "owner.json")
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        json_artifact.write_json_atomic(_window(root, record["window_key"]), record, indent=2)
        json_artifact.write_json_atomic(root / "active" / "owner.json", record, indent=2)
        return record


def finish(root: Path, reservation_id: str, thread_id: str, result: str, *, confirmed_terminal: bool) -> dict:
    if not confirmed_terminal or result not in {"completed", "failed", "waiting_for_user"}:
        raise ValueError("repair_dispatch_terminal_confirmation_required")
    with _lease(root):
        owner = _read(root / "active" / "owner.json")
        record = _read(_window(root, owner.get("window_key")))
        binding = ("schema_version", "kind", "window_key", "source_thread_id", "reservation_id", "thread_id")
        if (owner.get("reservation_id") != reservation_id or owner.get("thread_id") != thread_id
                or owner.get("status") != "running"
                or any(owner.get(key) != record.get(key) for key in binding)
                or record.get("status") not in {"running", result}):
            raise ValueError("repair_dispatch_owner_invalid")
        record = dict(owner)
        record.update(status=result, updated_at=datetime.now(timezone.utc).isoformat())
        json_artifact.write_json_atomic(_window(root, record["window_key"]), record, indent=2)
        os.rename(root / "active", root / ("closed-active-" + reservation_id))
        return record


def quarantine_orphan(root: Path, *, confirmed_no_active_task: bool) -> dict:
    if not confirmed_no_active_task:
        raise ValueError("repair_dispatch_orphan_confirmation_required")
    with _lease(root):
        active = root / "active"
        if active.is_symlink() or not active.is_dir() or list(active.iterdir()):
            raise ValueError("repair_dispatch_orphan_invalid")
        target = root / ("orphan-active-" + uuid.uuid4().hex)
        os.rename(active, target)
        return {"status": "orphan_preserved", "archive": str(target)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("status", "reserve", "bind", "progress", "finish", "quarantine-orphan"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--window-key")
    parser.add_argument("--source-thread-id", default="operator")
    parser.add_argument("--reservation-id")
    parser.add_argument("--thread-id")
    parser.add_argument("--result", choices=("completed", "failed", "waiting_for_user"))
    parser.add_argument("--confirm-terminal", choices=("YES",))
    parser.add_argument("--confirm-no-active-task", choices=("YES",))
    args = parser.parse_args(argv)
    root = args.root.expanduser().absolute()
    try:
        if args.command == "status": value = status(root)
        elif args.command == "reserve": value = reserve(root, args.window_key, args.source_thread_id)
        elif args.command == "bind": value = bind(root, args.reservation_id, args.thread_id)
        elif args.command == "progress": value = progress(root, args.reservation_id, args.thread_id)
        elif args.command == "finish":
            value = finish(root, args.reservation_id, args.thread_id, args.result,
                           confirmed_terminal=args.confirm_terminal == "YES")
        else: value = quarantine_orphan(root, confirmed_no_active_task=args.confirm_no_active_task == "YES")
        print(json.dumps(value, ensure_ascii=False)); return 0
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({"status": "blocked", "reason": type(error).__name__})); return 1


if __name__ == "__main__":
    raise SystemExit(main())
