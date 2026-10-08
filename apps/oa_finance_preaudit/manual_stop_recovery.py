"""Recover an evidence-bound, task-originated plain stop without writing OA."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import stat
import subprocess
from typing import Callable

from apps.oa_approval import runtime_control

BLOCKED = "manual_stop_recovery_blocked"
_STOP_PAYLOAD = b"stop_requested\n"


def _object(raw: bytes | str) -> dict:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(BLOCKED)
    return value


def _payload(record: dict) -> dict:
    value = record.get("payload")
    if not isinstance(value, dict):
        raise ValueError(BLOCKED)
    return value


def _private_bytes(path: Path, *, max_bytes: int = 32_000_000, private: bool = True) -> bytes:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
        or info.st_mode & (0o077 if private else 0o022)):
        raise ValueError(BLOCKED)
    raw = runtime_control.read_control_bytes(path, max_bytes=max_bytes)
    if raw is None:
        raise ValueError(BLOCKED)
    current = path.lstat()
    if (info.st_dev, info.st_ino, info.st_mtime_ns) != (
        current.st_dev, current.st_ino, current.st_mtime_ns
    ):
        raise ValueError(BLOCKED)
    return raw


def _bound_bytes(reference: dict, *, private: bool = True) -> bytes:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ValueError(BLOCKED)
    path = Path(str(reference["path"]))
    if not path.is_absolute():
        raise ValueError(BLOCKED)
    raw = _private_bytes(path, private=private)
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError(BLOCKED)
    return raw


def _source_revision(target: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=target, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    if (git("rev-parse", "--show-toplevel") != str(target)
        or git("branch", "--show-current") != "main"
        or git("status", "--porcelain")
        or len(git("worktree", "list", "--porcelain").split("worktree ")) != 2):
        raise ValueError(BLOCKED)
    return git("rev-parse", "HEAD")


def _aware_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(BLOCKED)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(BLOCKED) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(BLOCKED)
    return parsed


def _verify_original_exit(terminal: dict, manifest: dict, binding: dict, observed: datetime) -> None:
    """Verify the actual process result in the original tool event, not its wrapper."""
    raw = _bound_bytes(terminal["source_file"], private=False)
    if type(terminal.get("source_line")) is not int or terminal["source_line"] < 1:
        raise ValueError(BLOCKED)
    records = raw.splitlines()
    decoded = [_object(line) for line in records]
    metadata = [_payload(entry) for entry in decoded if entry.get("type") == "session_meta"]
    if len(metadata) != 1 or metadata[0].get("cwd") != binding["target_root"]:
        raise ValueError(BLOCKED)
    stop_source = binding.get("stop_source")
    if (not isinstance(stop_source, dict) or set(stop_source) != {"line", "call_id"}
        or type(stop_source["line"]) is not int
        or not 1 <= stop_source["line"] < terminal["source_line"]):
        raise ValueError(BLOCKED)
    stop_call = _payload(decoded[stop_source["line"] - 1])
    if (stop_call.get("type") != "custom_tool_call" or stop_call.get("name") != "exec"
        or stop_call.get("call_id") != stop_source["call_id"]
        or "runtime_control.request_stop()" not in stop_call.get("input", "")
        or binding["original_conflict_status"]["path"] not in stop_call.get("input", "")):
        raise ValueError(BLOCKED)
    stop_results = []
    for entry in decoded[stop_source["line"]:terminal["source_line"] - 1]:
        output = _payload(entry)
        if output.get("call_id") != stop_source["call_id"] or output.get("type") != "custom_tool_call_output":
            continue
        for block in output.get("output", []):
            try:
                envelope = json.loads(block["text"])
                fact = ast.literal_eval(envelope.get("output", ""))
            except (ValueError, SyntaxError, KeyError, TypeError):
                continue
            if (type(envelope.get("exit_code")) is int and envelope["exit_code"] == 0
                and isinstance(fact, dict) and fact.get("stop_requested") is True
                and fact.get("stop_reason", "absent") is None
                and fact.get("cause") == "production_reconciliation_blocked"
                and fact.get("status_file") == binding["original_conflict_status"]["path"]):
                stop_results.append(fact)
    if len(stop_results) != 1:
        raise ValueError(BLOCKED)
    if terminal["source_line"] > len(records):
        raise ValueError(BLOCKED)
    event = decoded[terminal["source_line"] - 1]
    payload = _payload(event)
    if (event.get("type") != "response_item" or payload.get("type") != "custom_tool_call_output"
        or payload.get("call_id") != terminal.get("source_call_id")):
        raise ValueError(BLOCKED)
    calls = []
    for item in decoded[:terminal["source_line"] - 1]:
        call = _payload(item)
        if call.get("call_id") == terminal["source_call_id"] and call.get("type") == "custom_tool_call":
            calls.append(call)
    if len(calls) != 1 or calls[0].get("name") != "exec":
        raise ValueError(BLOCKED)
    source = calls[0].get("input", "")
    pattern = r'text\(await tools\.write_stdin\(\{([^{}]+)\}\)\);'
    operations = list(re.finditer(pattern, source))
    if not operations or re.sub(pattern, "", source).strip():
        raise ValueError(BLOCKED)
    sessions = []
    for operation in operations:
        match = re.search(r'(?<![\w])session_id\s*:\s*([1-9][0-9]*)', operation[1])
        if match is None:
            raise ValueError(BLOCKED)
        sessions.append(match[1])
    session = str(terminal["process_session_id"])
    if sessions.count(session) != 1:
        raise ValueError(BLOCKED)
    envelopes = []
    if not isinstance(payload.get("output"), list):
        raise ValueError(BLOCKED)
    for block in payload["output"]:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            raise ValueError(BLOCKED)
        try:
            value = json.loads(block["text"])
        except ValueError:
            continue
        if isinstance(value, dict) and "exit_code" in value:
            envelopes.append(value)
    if len(envelopes) != len(sessions):
        raise ValueError(BLOCKED)
    envelope = envelopes[sessions.index(session)]
    if type(envelope.get("exit_code")) is not int or envelope["exit_code"] != terminal["exit_code"]:
        raise ValueError(BLOCKED)
    receipt = _object(envelope["output"])
    if (receipt.get("kind") != "preaudit_controller_execution" or receipt.get("state") != "exited"
        or receipt.get("exit_code") != terminal["exit_code"]
        or receipt.get("children_reaped") is not True
        or receipt.get("run_id") != manifest.get("run_id")
        or receipt.get("controller_instance_id") != manifest.get("controller_instance_id")
        or receipt.get("controller_pid") != manifest.get("controller_pid")
        or receipt.get("code_revision") != manifest.get("code_revision")
        or receipt.get("controller_state") != manifest.get("state")):
        raise ValueError(BLOCKED)
    if not (_aware_time(manifest.get("completed_at")) <= _aware_time(receipt.get("completed_at"))
            <= _aware_time(event.get("timestamp")) <= observed):
        raise ValueError(BLOCKED)
    original_manifest = Path(binding["original_manifest"]["path"])
    if (receipt.get("owner_task_id") != metadata[0].get("id")
        or not isinstance(metadata[0].get("id"), str)
        or original_manifest.parent.parent != Path(binding["target_root"]) / "runtime/control/executions"
        or original_manifest.name != "controller-manifest.json"
        or original_manifest.parent.name != receipt.get("invocation_id")
        or receipt.get("receipt_file") != str(original_manifest.with_name("execution.json"))):
        raise ValueError(BLOCKED)


def _original_evidence(target: Path, binding: dict) -> tuple[dict, dict]:
    if (not isinstance(binding, dict)
        or set(binding) != {"schema_version", "kind", "target_root", "target_revision",
                            "stop", "stop_source", "manifest", "original_manifest", "original_conflict_status", "original_status", "original_exit"}
        or binding.get("schema_version") != 1
        or binding.get("kind") != "preaudit_manual_stop_recovery_binding"
        or binding.get("target_root") != str(target)
        or binding.get("target_revision") != _source_revision(target)
        or not isinstance(binding.get("manifest"), dict)
        or binding["manifest"].get("path") != str(target / "runtime/control/rolling-controller-manifest.json")):
        raise ValueError(BLOCKED)
    manifest = _object(_bound_bytes(binding["manifest"]))
    if _object(_bound_bytes(binding["original_manifest"])) != manifest:
        raise ValueError(BLOCKED)
    original = _object(_bound_bytes(binding["original_status"]))
    conflict = _object(_bound_bytes(binding["original_conflict_status"]))
    terminal = _object(_bound_bytes(binding["original_exit"]))
    handoff = original.get("controller_handoff", {})
    if (manifest.get("kind") != "preaudit_controller_run_manifest"
        or manifest.get("state") not in {"completed", "failed"}
        or manifest.get("children_reaped") is not True
        or handoff.get("manifest_state") != manifest["state"]
        or handoff.get("children_reaped") is not True
        or handoff.get("controller_active") is not False
        or handoff.get("writer_active") is not False
        or type(handoff.get("active_request_writers")) is not int
        or handoff["active_request_writers"] != 0
        or handoff.get("code_revision") != manifest.get("code_revision")
        or original.get("kind") != "preaudit_runtime_status"
        or terminal.get("kind") != "preaudit_original_controller_exit"
        or terminal.get("run_id") != manifest.get("run_id")
        or terminal.get("terminal") is not True
        or terminal.get("children_reaped") is not True
        or type(terminal.get("exit_code")) is not int
        or re.fullmatch(r"[1-9][0-9]*", str(terminal.get("process_session_id"))) is None
        or (manifest["state"] == "completed" and terminal["exit_code"] != 0)
        or (manifest["state"] == "failed" and terminal["exit_code"] == 0)):
        raise ValueError(BLOCKED)
    completed = _aware_time(manifest.get("completed_at"))
    observed = _aware_time(original.get("generated_at"))
    if observed < completed:
        raise ValueError(BLOCKED)
    conflict_at = _aware_time(conflict.get("generated_at"))
    stop_at = datetime.fromtimestamp(binding["stop"]["mtime_ns"] / 1_000_000_000, tz=timezone.utc)
    if conflict_at.tzinfo is None or not conflict_at <= stop_at <= completed <= observed:
        raise ValueError(BLOCKED)
    from .maintenance_runtime import assert_live_safe
    assert_live_safe(original, maintenance_leases_held=False, original_closure=True)
    if (conflict.get("kind") != "preaudit_runtime_status"
        or conflict.get("reconciliation", {}).get("continuation", {}).get("status") != "blocked"
        or not any(row.get("category") == "pending_handled_conflict"
                   for row in conflict.get("reconciliation", {}).get("findings", []) if isinstance(row, dict))):
        raise ValueError(BLOCKED)
    _verify_original_exit(terminal, manifest, binding, observed)

    pid = manifest.get("controller_pid")
    if type(pid) is not int or pid <= 0:
        raise ValueError(BLOCKED)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise ValueError(BLOCKED)
    return manifest, terminal


def _original_stop(target: Path, binding: dict) -> tuple[int, int, int]:
    if str(os.environ.get("OA_FINANCE_PREAUDIT_STOP_REQUESTED") or "").strip().lower() in {"1", "true", "yes", "y", "on"}:
        raise ValueError(BLOCKED)
    stop = target / "runtime/stop-preaudit.flag"
    expected = binding.get("stop")
    if (not isinstance(expected, dict) or set(expected) != {"device", "inode", "mtime_ns", "sha256"}
        or any(type(expected[key]) is not int or expected[key] < 0 for key in ("device", "inode", "mtime_ns"))):
        raise ValueError(BLOCKED)
    info = stop.lstat()
    identity = (info.st_dev, info.st_ino, info.st_mtime_ns)
    raw = _private_bytes(stop, max_bytes=128)
    if (identity != (expected["device"], expected["inode"], expected["mtime_ns"])
        or raw != _STOP_PAYLOAD or hashlib.sha256(raw).hexdigest() != expected["sha256"]):
        raise ValueError(BLOCKED)
    return identity


def recover_manual_stop(
    *, target_root: Path, evidence_file: Path, confirmed: bool,
    check_live: Callable[[], dict] | None = None,
) -> dict:
    """Audit original terminal evidence, recheck live gates, then clear its marker."""
    from .maintenance_runtime import assert_live_safe, read_live_status, target_runtime_leases

    if confirmed is not True:
        raise ValueError(BLOCKED)
    target = Path(os.path.abspath(target_root))
    raw_binding = _private_bytes(evidence_file)
    binding = _object(raw_binding)
    clear_started = False
    receipt_file = None
    try:
        with target_runtime_leases(target):
            identity = _original_stop(target, binding)
            manifest, terminal = _original_evidence(target, binding)
            current = check_live() if check_live is not None else read_live_status(target)
            assert_live_safe(current, maintenance_leases_held=True)
            if (_private_bytes(evidence_file) != raw_binding
                or _original_stop(target, binding) != identity
                or _original_evidence(target, binding) != (manifest, terminal)):
                raise ValueError(BLOCKED)
            receipt_file = target / "runtime/control" / f"manual-stop-recovery-{secrets.token_hex(16)}.json"
            receipt = {"schema_version": 1, "kind": "preaudit_manual_stop_recovery_receipt",
                       "phase": "validated", "recorded_at": datetime.now(timezone.utc).isoformat(),
                       "binding_sha256": hashlib.sha256(raw_binding).hexdigest(), "binding": binding,
                       "original_run_id": manifest["run_id"], "original_exit_code": terminal["exit_code"],
                       "current_status_file": current.get("maintenance_status_file"),
                       "current_input_proof": current.get("maintenance_current_inputs"),
                       "live_continuation": current["reconciliation"]["continuation"],
                       "write_state": "not_requested", "stop_cleared": False}
            def record() -> None:
                runtime_control.write_control_bytes_atomic(receipt_file, (json.dumps(receipt, sort_keys=True) + "\n").encode())
            record()
            if (_private_bytes(evidence_file) != raw_binding
                or _original_stop(target, binding) != identity
                or _original_evidence(target, binding) != (manifest, terminal)):
                raise ValueError(BLOCKED)
            writer = target / "runtime/control/frozen-writer.lock"
            if runtime_control.writer_active(writer) or runtime_control.active_request_writers(writer) != 1:
                raise ValueError(BLOCKED)
            clear_started = True
            cleared = runtime_control.clear_manual_stop(target=target / "runtime/stop-preaudit.flag", expected_identity=identity)
            receipt.update(phase="cleared" if cleared else "clear_rejected", stop_cleared=cleared)
            try:
                record()
            except (OSError, RuntimeError):
                return {"status": "audit_completion_failed", "stop_cleared": cleared,
                        "receipt_file": str(receipt_file), "write_state": "not_requested"}
            return {"status": "recovered" if cleared else "blocked", "stop_cleared": cleared,
                    "receipt_file": str(receipt_file), "write_state": "not_requested"}

    except (OSError, RuntimeError):
        if not clear_started or receipt_file is None:
            raise
        return {"status": "maintenance_completion_failed",
                "stop_cleared": not os.path.lexists(target / "runtime/stop-preaudit.flag"),
                "receipt_file": str(receipt_file), "write_state": "not_requested"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--confirm-reconciled", choices=("YES",), required=True)
    parser.add_argument("--expected-candidate-revision", required=True)
    args = parser.parse_args(argv)
    try:
        from .maintenance_runtime import assert_target_environment

        assert_target_environment(args.target_root)
        candidate = Path(__file__).resolve().parents[2]
        def git(*arguments: str) -> str:
            return subprocess.check_output(["git", *arguments], cwd=candidate,
                                           text=True, stderr=subprocess.DEVNULL).strip()
        if (re.fullmatch(r"[0-9a-f]{40}", args.expected_candidate_revision) is None
            or git("rev-parse", "HEAD") != args.expected_candidate_revision
            or git("rev-parse", "origin/main") != args.expected_candidate_revision
            or git("status", "--porcelain")):
            raise ValueError(BLOCKED)
        result = recover_manual_stop(target_root=args.target_root, evidence_file=args.evidence_file,
                                     confirmed=args.confirm_reconciled == "YES")
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
        result = {"status": "blocked", "error_code": BLOCKED,
                  "stop_cleared": False, "write_state": "not_requested"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "recovered" else 1


if __name__ == "__main__":
    raise SystemExit(main())
