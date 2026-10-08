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
import stat
import subprocess
import sys
import time
from typing import Sequence
from uuid import UUID, uuid4

from apps.oa_approval import auth, runtime_control
from apps.oa_finance_preaudit import controller_handoff

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_ENV = "OA_CONTROLLER_EXECUTION_MANIFEST_FILE"
EVIDENCE_LIMIT = 1_048_576
LOG_LIMIT = 64 * EVIDENCE_LIMIT


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _object(raw: bytes | None) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("controller_execution_evidence_failed")
            value[key] = item
        return value
    value = json.loads(raw or b"null", object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("controller_execution_evidence_failed")
    return value


def _timestamp(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("controller_execution_evidence_failed")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("controller_execution_evidence_failed")
    return stamp


def _read_evidence(path: Path) -> bytes | None:
    # Nonblocking open rejects FIFOs before any read; reuse the existing no-link directory protection.
    with runtime_control.pinned_directory(path.parent) as parent:
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > EVIDENCE_LIMIT:
                raise ValueError("controller_execution_evidence_failed")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(EVIDENCE_LIMIT + 1)
            after = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if (len(raw) > EVIDENCE_LIMIT or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                raise ValueError("controller_execution_snapshot_changed")
            return raw
        finally:
            os.close(descriptor)


def _log_evidence(path: Path) -> tuple[dict, bytes | None]:
    """Hash terminal logs in chunks; retain only a bounded stdout candidate."""
    with runtime_control.pinned_directory(path.parent) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > LOG_LIMIT:
                raise ValueError("controller_execution_output_limit")
            digest = hashlib.sha256()
            content = bytearray() if before.st_size <= EVIDENCE_LIMIT else None
            size = 0
            while True:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    break
                size += len(chunk)
                if size > LOG_LIMIT:
                    raise ValueError("controller_execution_output_limit")
                digest.update(chunk)
                if content is not None:
                    if size > EVIDENCE_LIMIT:
                        raise ValueError("controller_execution_snapshot_changed")
                    content.extend(chunk)
            after = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if (size != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                raise ValueError("controller_execution_snapshot_changed")
            return {"sha256": digest.hexdigest(), "size_bytes": size}, bytes(content) if content is not None else None
        finally:
            os.close(descriptor)


def _verified_skip(payload: dict) -> bool:
    handoff = payload.get("controller_handoff", {})
    return (payload.get("kind") == "preaudit_production_rolling_drain"
            and payload.get("status") == "skipped" and payload.get("reason") == "production_rolling_controller_active"
            and isinstance(handoff, dict) and handoff.get("status") == "active" and handoff.get("manifest_state") == "running"
            and all(handoff.get(key) is True for key in (
                "controller_active", "lock_owner_consistent", "restart_verified", "code_matches_current")))


def read_execution(receipt_file: Path, *, owner_task_id: str, invocation_id: str,
                   tool_session_id: str | None = None, retrieve: bool = False) -> dict:
    """Project one owned execution; reading never starts or controls a process."""
    path = Path(os.path.abspath(receipt_file))
    if (path.name != "execution.json" or path.parent.parent != controller_handoff.manifest_file().parent / "executions"
            or controller_handoff._RUN_ID.fullmatch(invocation_id) is None or path.parent.name != invocation_id):
        raise ValueError("controller_execution_identity_mismatch")
    with runtime_control.pinned_directory(path.parent):
        raw = _read_evidence(path)
        receipt = _object(raw)
        if (receipt.get("kind") != "preaudit_controller_execution"
                or type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1
                or receipt.get("owner_task_id") != owner_task_id or receipt.get("invocation_id") != invocation_id
                or receipt.get("state") not in {"starting", "waiting", "exited", "start_failed"}):
            raise ValueError("controller_execution_identity_mismatch")
        started = _timestamp(receipt["started_at"])
        if (started > datetime.now(timezone.utc)
                or (receipt.get("children_reaped") != "unknown" and type(receipt.get("children_reaped")) is not bool)
                or (receipt["state"] in {"waiting", "exited"} and (
                    type(receipt.get("controller_pid")) is not int or receipt["controller_pid"] <= 0
                    or type(receipt.get("process_group_id")) is not int
                    or receipt["process_group_id"] != receipt["controller_pid"]))):
            raise ValueError("controller_execution_evidence_failed")
        binding_raw = _read_evidence(path.with_name("tool-session.json"))
        session = "unknown"
        if binding_raw is not None:
            binding = _object(binding_raw)
            session = binding.get("tool_session_id")
            if (binding.get("kind") != "preaudit_controller_tool_session"
                    or binding.get("owner_task_id") != owner_task_id or binding.get("invocation_id") != invocation_id
                    or binding.get("evidence_source") != "caller_reported_tool_result"
                    or not isinstance(session, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", session) is None):
                raise ValueError("controller_execution_identity_mismatch")
        if tool_session_id is not None and (binding_raw is None or session != tool_session_id):
            raise ValueError("controller_execution_identity_mismatch")
        state = receipt["state"]
        report = {"kind": "preaudit_controller_execution_readback", "read_only": True,
                  "invocation_id": invocation_id, "owner_task_id": owner_task_id,
                  "tool_session_id": session, "state": state, "terminal": False,
                  "status": "waiting" if state in {"starting", "waiting"} else "blocked",
                  "controller_state": "unknown", "children_reaped": "unknown",
                  "exit_code": receipt.get("exit_code"), "exit_signal": receipt.get("exit_signal"),
                  "receipt_file": str(path)}
        manifest_raw = _read_evidence(path.with_name("controller-manifest.json"))
        manifest = None
        if manifest_raw is not None:
            manifest = _object(manifest_raw)
            if (manifest.get("kind") != "preaudit_controller_run_manifest"
                    or ("schema_version" in manifest and (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1))
                    or type(receipt.get("controller_pid")) is not int or receipt["controller_pid"] <= 0
                    or type(manifest.get("controller_pid")) is not int
                    or manifest.get("controller_pid") != receipt["controller_pid"]
                    or not started <= _timestamp(manifest["started_at"]) <= _timestamp(receipt["completed_at"] or _now())
                    or manifest.get("state") not in {"running", "completed", "failed"}
                    or type(manifest.get("children_reaped")) is not bool
                    or any(pattern.fullmatch(str(manifest.get(key, ""))) is None for key, pattern in (
                        ("run_id", controller_handoff._RUN_ID), ("controller_instance_id", controller_handoff._RUN_ID),
                        ("code_revision", controller_handoff._REVISION)))):
                raise ValueError("controller_execution_manifest_mismatch")
            report.update(controller_state=manifest["state"], children_reaped=manifest["children_reaped"],
                          run_id=manifest["run_id"], controller_instance_id=manifest["controller_instance_id"],
                          code_revision=manifest["code_revision"])
        if state == "exited" and "result" in receipt:
            if (type(receipt.get("exit_code")) is not int
                    or receipt.get("exit_signal") != (-receipt["exit_code"] if receipt["exit_code"] < 0 else None)
                    or not started <= _timestamp(receipt["completed_at"]) <= datetime.now(timezone.utc)):
                raise ValueError("controller_execution_evidence_failed")
            if not isinstance(receipt.get("output_sha256"), dict):
                raise ValueError("controller_execution_output_mismatch")
            logs = {}
            output_files = {}
            for name in ("stdout.log", "stderr.log"):
                fingerprint, content = _log_evidence(path.with_name(name))
                if fingerprint["sha256"] != receipt["output_sha256"].get(name):
                    raise ValueError("controller_execution_output_mismatch")
                logs[name] = content
                output_files[name] = fingerprint
            report["output_files"] = output_files
            if manifest is not None:
                if any(receipt.get(key) != manifest[source] for key, source in (
                        ("run_id", "run_id"), ("controller_instance_id", "controller_instance_id"),
                        ("controller_state", "state"), ("children_reaped", "children_reaped"),
                        ("code_revision", "code_revision"))):
                    raise ValueError("controller_execution_manifest_mismatch")
                if (receipt.get("manifest_sha256") is not None
                        and hashlib.sha256(manifest_raw).hexdigest() != receipt["manifest_sha256"]):
                    raise ValueError("controller_execution_manifest_mismatch")
                report["terminal"] = manifest["state"] in {"completed", "failed"} and manifest["children_reaped"] is True
            if (report["terminal"] and receipt["exit_code"] == 0 and report["controller_state"] == "completed"
                    and receipt["result"] == "completed" and "stop_request_error" not in receipt):
                report["status"] = "completed"
            elif (manifest is None and receipt["exit_code"] == 0 and receipt["result"] == "skipped"
                    and "stop_request_error" not in receipt and logs["stdout.log"] is not None
                    and _verified_skip(_object(logs["stdout.log"]))):
                report.update(status="skipped", terminal=True)
            if retrieve and report["terminal"]:
                if logs["stdout.log"] is None:
                    report.update(output_omitted="size_limit", controller_stdout_file=str(path.with_name("stdout.log")))
                else:
                    report["controller_stdout"] = logs["stdout.log"].decode("utf-8")
                report["output_sha256"] = receipt["output_sha256"]
        # Atomic receipt replacement while the child exits requires a fresh snapshot, never mixed proof.
        if (_read_evidence(path) != raw
                or _read_evidence(path.with_name("controller-manifest.json")) != manifest_raw
                or _read_evidence(path.with_name("tool-session.json")) != binding_raw):
            raise ValueError("controller_execution_snapshot_changed")
    return report


def wait_execution(receipt_file: Path, *, wait_seconds: float = 0, **options) -> dict:
    if not 0 <= wait_seconds <= 60:
        raise ValueError("controller_execution_wait_invalid")
    deadline = time.monotonic() + wait_seconds
    while True:
        report = read_execution(receipt_file, **options)
        remaining = deadline - time.monotonic()
        if report["terminal"] or report["status"] != "waiting" or remaining <= 0:
            return report
        time.sleep(min(0.25, remaining))


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
            receipt["manifest_sha256"] = hashlib.sha256(raw_manifest).hexdigest()
        receipt["result"] = (
            "completed" if receipt["state"] == "exited" and receipt["exit_code"] == 0
            and receipt["controller_state"] == "completed" and receipt["children_reaped"] is True
            and "stop_request_error" not in receipt
            else "blocked"
        )
        if receipt["state"] == "exited" and receipt["exit_code"] == 0 and mirror_missing:
            try:
                payload = json.loads(runtime_control.read_control_bytes(directory / "stdout.log") or b"null")
                verified_skip = isinstance(payload, dict) and _verified_skip(payload)
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
    reads = parser.add_mutually_exclusive_group()
    reads.add_argument("--progress", action="store_true")
    reads.add_argument("--retrieve-result", action="store_true")
    parser.add_argument("--invocation-id")
    parser.add_argument("--tool-session-id")
    parser.add_argument("--wait-seconds", type=float, default=0)
    parser.add_argument("controller_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    arguments = args.controller_args
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    directory = None
    try:
        if args.progress or args.retrieve_result:
            if args.receipt_file is None or args.invocation_id is None or args.bind_tool_session is not None or arguments:
                raise ValueError("controller_execution_readback_invalid")
            result = wait_execution(args.receipt_file, owner_task_id=str(args.owner_task_id),
                                    invocation_id=args.invocation_id, tool_session_id=args.tool_session_id,
                                    wait_seconds=args.wait_seconds, retrieve=args.retrieve_result)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result["status"] in {"completed", "skipped"} else (2 if result["status"] == "waiting" else 1)
        if args.invocation_id is not None or args.tool_session_id is not None or args.wait_seconds:
            raise ValueError("controller_execution_readback_invalid")
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
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        print(json.dumps({"error_code": "controller_execution_evidence_failed", "receipt_file": str(directory / "execution.json") if directory else None}))
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["result"] in {"completed", "skipped"} else 1


if __name__ == "__main__":
    sys.exit(main())
