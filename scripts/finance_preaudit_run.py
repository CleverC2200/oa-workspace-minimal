"""Supervise the existing controller and retain its actual process result."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from typing import Sequence
from uuid import UUID, uuid4

from apps.oa_approval import auth, runtime_control
from apps.oa_finance_preaudit import controller_handoff

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_ENV = "OA_CONTROLLER_EXECUTION_MANIFEST_FILE"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bind_tool_session(receipt_file: Path, *, owner_task_id: str, session_id: str) -> dict:
    """Retain the caller's tool ID separately so the waiting supervisor cannot overwrite it."""
    UUID(owner_task_id)
    path = Path(os.path.abspath(receipt_file))
    if (path.name != "execution.json" or path.parent.parent != controller_handoff.manifest_file().parent / "executions"
            or controller_handoff._RUN_ID.fullmatch(path.parent.name) is None
            or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", session_id) is None):
        raise ValueError("controller_tool_session_binding_invalid")
    receipt = json.loads(runtime_control.read_control_bytes(path) or b"null")
    if (not isinstance(receipt, dict) or receipt.get("kind") != "preaudit_controller_execution"
            or receipt.get("owner_task_id") != owner_task_id or receipt.get("invocation_id") != path.parent.name):
        raise ValueError("controller_tool_session_binding_invalid")
    value = {"kind": "preaudit_controller_tool_session", "owner_task_id": owner_task_id,
             "invocation_id": path.parent.name, "tool_session_id": session_id,
             "evidence_source": "caller_reported_tool_result"}
    with runtime_control.pinned_directory(path.parent) as parent:
        try:
            descriptor = os.open("tool-session.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        except FileExistsError:
            if json.loads(runtime_control.read_control_bytes(path.with_name("tool-session.json")) or b"null") != value:
                raise ValueError("controller_tool_session_binding_conflict") from None
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write((json.dumps(value) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(parent)
    return {"status": "bound", "tool_session_file": str(path.with_name("tool-session.json"))}


def supervise(command: Sequence[str], *, directory: Path, owner_task_id: str) -> dict:
    """Wait for one owned process; controller cleanup remains controller-owned."""
    UUID(owner_task_id)
    directory = Path(os.path.abspath(directory))
    with runtime_control.pinned_directory(directory.parent, create=True) as parent:
        os.mkdir(directory.name, mode=0o700, dir_fd=parent)
    receipt_file = directory / "execution.json"
    manifest_file = directory / "controller-manifest.json"
    receipt = {
        "schema_version": 1, "kind": "preaudit_controller_execution",
        "invocation_id": directory.name, "owner_task_id": owner_task_id,
        "tool_session_id": "unknown", "supervisor_pid": os.getpid(),
        "controller_pid": None, "process_group_id": None,
        "started_at": _now(), "completed_at": None, "state": "starting",
        "exit_code": None, "exit_signal": None, "received_signals": [],
        "stop_requested_by_supervisor": False,
        "controller_state": "unknown", "children_reaped": "unknown",
        "run_id": None, "controller_instance_id": None,
        "tool_session_file": str(directory / "tool-session.json"),
    }
    old_umask = os.umask(0o077)
    process = None
    handlers = {}
    stop_attempted = False

    def persist() -> None:
        runtime_control.write_control_bytes_atomic(
            receipt_file, (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode(),
        )

    def forward(signum: int, _frame: object) -> None:
        receipt["received_signals"].append(signum)
        persist()


    def wait_owned(*, cancel: bool = False) -> int:
        nonlocal stop_attempted
        while True:
            if ((cancel or receipt["received_signals"])
                    and not stop_attempted
                    and runtime_control.controller_owner_pid() == process.pid):
                stop_attempted = True
                try:
                    runtime_control.request_cycle_stop()
                except (OSError, RuntimeError, ValueError):
                    receipt["stop_request_error"] = "controller_stop_request_failed"
                else:
                    receipt["stop_requested_by_supervisor"] = True
            try:
                return process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                continue

    def open_log(name: str):
        with runtime_control.pinned_directory(directory) as parent:
            descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        return os.fdopen(descriptor, "w+b")

    try:
        persist()
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            handlers[signum] = signal.signal(signum, forward)
        environment = {**os.environ, MANIFEST_ENV: str(manifest_file)}
        with open_log("stdout.log") as stdout, open_log("stderr.log") as stderr:
            try:
                if receipt["received_signals"]:
                    raise InterruptedError("controller_start_cancelled")
                process = subprocess.Popen(
                    list(command), cwd=PROJECT_ROOT, env=environment,
                    stdout=stdout, stderr=stderr, start_new_session=True,
                )
            except OSError as error:
                receipt.update(state="start_failed", error_type=type(error).__name__, completed_at=_now())
            else:
                receipt.update(state="waiting", controller_pid=process.pid, process_group_id=process.pid)
                persist()
                print(json.dumps({"state": "waiting", "receipt_file": str(receipt_file),
                                  "owner_task_id": owner_task_id, "controller_pid": process.pid}), flush=True)
                # Silence, elapsed time and warnings are not terminal evidence.
                code = wait_owned()
                receipt.update(state="exited", exit_code=code,
                               exit_signal=-code if code < 0 else None, completed_at=_now())
                persist()
            receipt["output_sha256"] = {}
            for name, stream in (("stdout.log", stdout), ("stderr.log", stderr)):
                stream.flush()
                os.fsync(stream.fileno())
                stream.seek(0)
                receipt["output_sha256"][name] = hashlib.file_digest(stream, "sha256").hexdigest()
        mirror_missing = False
        try:
            raw_manifest = runtime_control.read_control_bytes(manifest_file)
            mirror_missing = raw_manifest is None
            manifest = json.loads(raw_manifest or b"null")
            if not isinstance(manifest, dict):
                raise ValueError("controller_manifest_invalid")
            started = datetime.fromisoformat(manifest["started_at"])
            matched = (
                manifest.get("kind") == "preaudit_controller_run_manifest"
                and manifest.get("controller_pid") == receipt["controller_pid"]
                and started.tzinfo is not None
                and started >= datetime.fromisoformat(receipt["started_at"])
                and manifest.get("state") in {"running", "completed", "failed"}
                and type(manifest.get("children_reaped")) is bool
                and controller_handoff._REVISION.fullmatch(str(manifest.get("code_revision", "")))
                and controller_handoff._RUN_ID.fullmatch(str(manifest.get("run_id", "")))
                and controller_handoff._RUN_ID.fullmatch(str(manifest.get("controller_instance_id", "")))
            )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            matched = False
        if matched:
            receipt.update(run_id=manifest["run_id"], controller_instance_id=manifest["controller_instance_id"],
                           controller_state=manifest["state"], children_reaped=manifest["children_reaped"],
                           code_revision=manifest["code_revision"])
        receipt["result"] = (
            "completed" if receipt["state"] == "exited" and receipt["exit_code"] == 0
            and receipt["controller_state"] == "completed" and receipt["children_reaped"] is True
            and "stop_request_error" not in receipt
            else "blocked"
        )
        if receipt["state"] == "exited" and receipt["exit_code"] == 0 and mirror_missing:
            try:
                payload = json.loads(runtime_control.read_control_bytes(directory / "stdout.log") or b"null")
                handoff = payload.get("controller_handoff", {}) if isinstance(payload, dict) else {}
                verified_skip = (
                    payload.get("kind") == "preaudit_production_rolling_drain"
                    and payload.get("status") == "skipped"
                    and payload.get("reason") == "production_rolling_controller_active"
                    and isinstance(handoff, dict)
                    and handoff.get("status") == "active"
                    and handoff.get("manifest_state") == "running"
                    and all(handoff.get(key) is True for key in (
                        "controller_active", "lock_owner_consistent", "restart_verified", "code_matches_current",
                    ))
                )
            except (OSError, RuntimeError, ValueError, AttributeError):
                verified_skip = False
            if verified_skip and "stop_request_error" not in receipt:
                receipt["result"] = "skipped"
        persist()
        return {**receipt, "receipt_file": str(receipt_file)}
    finally:
        try:
            if process is not None and process.poll() is None:
                # An internal supervisor failure cannot abandon its direct child.
                code = wait_owned(cancel=True)
                receipt.update(state="exited", exit_code=code,
                               exit_signal=-code if code < 0 else None, completed_at=_now())
                persist()
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
            os.umask(old_umask)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-task-id", required=True, type=UUID)
    parser.add_argument("--bind-tool-session")
    parser.add_argument("--receipt-file", type=Path)
    parser.add_argument("controller_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    arguments = args.controller_args
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    directory = None
    try:
        auth.load_local_env()
        if args.bind_tool_session is not None:
            if args.receipt_file is None or arguments:
                raise ValueError("controller_tool_session_binding_invalid")
            result = bind_tool_session(args.receipt_file, owner_task_id=str(args.owner_task_id), session_id=args.bind_tool_session)
            print(json.dumps(result))
            return 0
        if args.receipt_file is not None:
            raise ValueError("controller_tool_session_binding_invalid")
        directory = controller_handoff.manifest_file().parent / "executions" / uuid4().hex
        receipt = supervise(
            [sys.executable, "-m", "scripts.finance_preaudit_concurrency_probe", *arguments],
            directory=directory, owner_task_id=str(args.owner_task_id),
        )
    except (OSError, RuntimeError, ValueError):
        print(json.dumps({"error_code": "controller_execution_evidence_failed", "receipt_file": str(directory / "execution.json") if directory else None}))
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["result"] in {"completed", "skipped"} else 1


if __name__ == "__main__":
    sys.exit(main())
