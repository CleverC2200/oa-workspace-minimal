from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from typing import Callable, Mapping

from apps.oa_approval import runtime_control


SCHEMA_VERSION = 1
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_STATES = {"running", "completed", "failed"}
_HEALTH = {"healthy", "degraded", "unknown"}
_RECOVERY_MIN_AGE_SECONDS = 30
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTROLLER_INSTANCE_ID = secrets.token_hex(16)


def _recovery_reconciliation_is_trusted(reconciled: Mapping[str, object]) -> bool:
    """Allow orphan cleanup while deferring proven historical no-write failures."""
    continuation = reconciled.get("continuation")
    quarantine = reconciled.get("quarantine")
    if not isinstance(continuation, Mapping) or not isinstance(quarantine, Mapping):
        return False
    if continuation == {
        "status": "ready", "scope": "none", "isolated_pending": 0,
        "blocking_reasons": {},
    } and quarantine.get("pending_quarantined") == 0:
        return True
    isolated = continuation.get("isolated_pending")
    if (continuation.get("status") != "ready"
        or continuation.get("scope") != "item"
        or continuation.get("blocking_reasons") != {}
        or type(isolated) is not int or isolated <= 0
        or type(quarantine.get("pending_quarantined")) is not int
        or isolated != quarantine.get("pending_quarantined")
        or set(continuation) != {"status", "scope", "isolated_pending", "blocking_reasons"}):
        return False
    return True


def manifest_file() -> Path:
    lock_file = runtime_control.controller_lock_file().expanduser()
    if not lock_file.is_absolute():
        lock_file = PROJECT_ROOT / lock_file
    return Path(os.path.abspath(lock_file)).with_name(
        "rolling-controller-manifest.json"
    )


def _code_state() -> tuple[str, bool | str]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown", "unknown"
    raw_revision = revision.stdout.strip()
    if revision.returncode != 0 or _REVISION.fullmatch(raw_revision) is None:
        return "unknown", "unknown"
    if status.returncode != 0:
        return raw_revision, "unknown"
    return raw_revision, bool(status.stdout)


def _config_fingerprint(config: Mapping[str, object]) -> str:
    raw = json.dumps(
        dict(config),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _read_manifest() -> dict | None:
    path = manifest_file()
    try:
        raw = runtime_control.read_control_bytes(path)
    except RuntimeError as error:
        raise ValueError("controller_handoff_untrusted") from error
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("controller_handoff_untrusted") from error
    expected_fields = {
        "schema_version",
        "kind",
        "state",
        "run_id",
        "started_at",
        "completed_at",
        "controller_pid",
        "controller_instance_id",
        "previous_run_id",
        "previous_controller_instance_id",
        "code_revision",
        "code_dirty",
        "config_fingerprint",
        "restart_mode",
        "controller_lock_verified",
        "writer_inactive_at_start",
        "children_reaped",
        "restart_verified",
        "health",
    }
    if isinstance(value, dict) and "review_configuration" in value:
        from . import production_config
        configuration = value["review_configuration"]
        if (not production_config.valid_review_configuration(configuration)):
            raise ValueError("controller_handoff_untrusted")
        expected_fields.add("review_configuration")
    if (
        not isinstance(value, dict)
        or set(value) != expected_fields
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("kind") != "preaudit_controller_run_manifest"
        or value.get("state") not in _STATES
        or _RUN_ID.fullmatch(str(value.get("run_id") or "")) is None
        or not isinstance(value.get("started_at"), str)
        or not isinstance(value.get("controller_pid"), int)
        or isinstance(value.get("controller_pid"), bool)
        or value["controller_pid"] < 1
        or _RUN_ID.fullmatch(
            str(value.get("controller_instance_id") or "")
        )
        is None
        or (
            value.get("previous_run_id") is not None
            and _RUN_ID.fullmatch(str(value.get("previous_run_id") or ""))
            is None
        )
        or (
            value.get("previous_controller_instance_id") is not None
            and _RUN_ID.fullmatch(
                str(value.get("previous_controller_instance_id") or "")
            )
            is None
        )
        or (
            (value.get("previous_run_id") is None)
            != (value.get("previous_controller_instance_id") is None)
        )
        or (
            value.get("code_revision") != "unknown"
            and _REVISION.fullmatch(str(value.get("code_revision") or "")) is None
        )
        or value.get("code_dirty") not in {True, False, "unknown"}
        or _FINGERPRINT.fullmatch(
            str(value.get("config_fingerprint") or "")
        )
        is None
        or value.get("restart_mode") != "fresh_process"
        or value.get("controller_lock_verified") is not True
        or value.get("writer_inactive_at_start") is not True
        or not isinstance(value.get("children_reaped"), bool)
        or value.get("restart_verified") is not True
        or value.get("health") not in _HEALTH
        or (
            value.get("state") == "running"
            and value.get("completed_at") is not None
        )
        or (
            value.get("state") != "running"
            and not isinstance(value.get("completed_at"), str)
        )
    ):
        raise ValueError("controller_handoff_untrusted")
    try:
        started_at = datetime.fromisoformat(value["started_at"])
        completed_at = (
            datetime.fromisoformat(value["completed_at"])
            if value["completed_at"] is not None
            else None
        )
    except ValueError as error:
        raise ValueError("controller_handoff_untrusted") from error
    if (
        started_at.tzinfo is None
        or (completed_at is not None and completed_at.tzinfo is None)
        or (completed_at is not None and completed_at < started_at)
        or (value["state"] == "running" and value["children_reaped"] is True)
    ):
        raise ValueError("controller_handoff_untrusted")
    return value


def assert_ready_for_start() -> None:
    manifest = _read_manifest()
    if manifest is None or runtime_control.controller_active():
        return
    if (
        manifest["state"] not in {"completed", "failed"}
        or manifest["children_reaped"] is not True
        or manifest["controller_instance_id"] == CONTROLLER_INSTANCE_ID
    ):
        raise ValueError("controller_handoff_untrusted")


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def recover_confirmed_children_reaped(*, confirmed: bool) -> dict:
    """Repair one stale terminal handoff after an operator verified reaping."""
    manifest = _read_manifest()
    revision, dirty = _code_state()
    now = datetime.now(timezone.utc)
    try:
        completed_at = datetime.fromisoformat(
            str(manifest["completed_at"] if manifest else "")
        )
    except ValueError as error:
        raise ValueError("controller_handoff_recovery_blocked") from error
    if (
        confirmed is not True
        or manifest is None
        or runtime_control.stop_reason() != "controller_cycle_stop"
        or manifest["state"] != "failed"
        or manifest["children_reaped"] is not False
        or manifest["controller_instance_id"] == CONTROLLER_INSTANCE_ID
        or completed_at.tzinfo is None
        or (now - completed_at).total_seconds() < _RECOVERY_MIN_AGE_SECONDS
        or _process_alive(manifest["controller_pid"])
        or runtime_control.controller_active()
        or runtime_control.writer_active()
        or runtime_control.active_request_writers() != 0
        or revision == "unknown"
        or dirty is not False
    ):
        raise ValueError("controller_handoff_recovery_blocked")
    recovered = {**manifest, "children_reaped": True}
    _write_manifest(recovered)
    return {
        "status": "recovered",
        "manifest_state": recovered["state"],
        "children_reaped": True,
        "health": recovered["health"],
    }


def recover_orphaned_run(
    *, confirmed_children_reaped: bool, check_live: Callable[[], dict],
    mode: str = "orphaned",
) -> dict:
    """Close an abandoned run as failed, never resume its business attempts.

    The operator must verify the original process tree has been reaped. A dead
    controller PID alone cannot establish that detached children have exited.
    """
    blocked = "controller_handoff_recovery_blocked"
    if confirmed_children_reaped is not True or mode not in {"orphaned", "failed"}:
        raise ValueError(blocked)
    expected_state = "failed" if mode == "failed" else "running"

    def stop_snapshot() -> tuple | None:
        if mode == "orphaned":
            if runtime_control.stop_requested():
                raise ValueError(blocked)
            return None
        if (runtime_control.stop_reason() != "controller_cycle_stop"
            or str(os.environ.get("OA_FINANCE_PREAUDIT_STOP_REQUESTED", "")).strip().lower()
            in {"1", "true", "yes", "y", "on"}):
            raise ValueError(blocked)
        path = runtime_control.stop_file()
        pinned = path.lstat()
        raw = runtime_control.read_control_bytes(path)
        if raw != b"stop_requested:controller_cycle_stop\n":
            raise ValueError(blocked)
        return pinned.st_dev, pinned.st_ino, pinned.st_mtime_ns, raw

    try:
        with runtime_control.global_controller_lease():
            with runtime_control.request_writer_lease(
                runtime_control.GLOBAL_WRITER_KEY,
                runtime_control.writer_lock_file(), blocking=False,
            ):
                manifest = _read_manifest()
                revision, dirty = _code_state()
                original_stop = stop_snapshot()
                if (manifest is None or manifest["state"] != expected_state
                    or manifest["children_reaped"] is not False
                    or manifest["controller_instance_id"] == CONTROLLER_INSTANCE_ID
                    or _process_alive(manifest["controller_pid"])
                    or revision == "unknown" or dirty is not False
                    or runtime_control.writer_active()
                    or runtime_control.active_request_writers() != 1):
                    raise ValueError(blocked)
                started = datetime.fromisoformat(manifest[
                    "completed_at" if mode == "failed" else "started_at"
                ])
                if (started.tzinfo is None or
                    (datetime.now(timezone.utc) - started).total_seconds() < _RECOVERY_MIN_AGE_SECONDS):
                    raise ValueError(blocked)
                snapshot = check_live()
                live = snapshot.get("live_check", {})
                reconciled = snapshot.get("reconciliation", {})
                if (live.get("credentials", {}).get("status") != "ok"
                    or live.get("oa", {}).get("execution_identity") != "verified"
                    or not _recovery_reconciliation_is_trusted(reconciled)
                    or _read_manifest() != manifest
                    or stop_snapshot() != original_stop
                    or _code_state() != (revision, False)
                    or _process_alive(manifest["controller_pid"])
                    or runtime_control.writer_active()
                    or runtime_control.active_request_writers() != 1):
                    raise ValueError(blocked)
                evidence = manifest_file().with_name(
                    f"orphan-controller-{manifest['run_id']}.json"
                )
                raw = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
                existing = runtime_control.read_control_bytes(evidence)
                if existing is not None and existing != raw:
                    raise ValueError(blocked)
                runtime_control.write_control_bytes_atomic(evidence, raw)
                stop_evidence = evidence.with_suffix(".stop.flag")
                if original_stop is not None:
                    existing_stop = runtime_control.read_control_bytes(stop_evidence)
                    if existing_stop not in {None, original_stop[3]}:
                        raise ValueError(blocked)
                    runtime_control.write_control_bytes_atomic(stop_evidence, original_stop[3])
                if stop_snapshot() != original_stop:
                    raise ValueError(blocked)
                if original_stop is None:
                    result = finish(manifest, status="failed", children_reaped=True, health="degraded")
                else:
                    recovered = {**manifest, "children_reaped": True}
                    try:
                        _write_manifest(recovered)
                        if not runtime_control.clear_cycle_stop(expected_identity=original_stop[:3]):
                            raise ValueError(blocked)
                    except BaseException:
                        # Keep a failed maintenance attempt retryable, without
                        # overwriting a concurrently replaced manifest or stop.
                        if _read_manifest() == recovered:
                            _write_manifest(manifest)
                        runtime_control.request_cycle_stop()
                        raise
                    result = {"manifest_state": "failed", "children_reaped": True,
                              "health": manifest["health"]}
                return {**result, "status": "recovered", "evidence_file": str(evidence),
                        **({"stop_evidence_file": str(stop_evidence)} if original_stop else {}),
                        "write_state": "not_requested"}
    except (RuntimeError, TypeError, KeyError, AttributeError) as error:
        raise ValueError(blocked) from error


def _write_manifest(manifest: Mapping[str, object]) -> None:
    payload = (
        json.dumps(
            dict(manifest),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    try:
        runtime_control.write_control_bytes_atomic(manifest_file(), payload)
    except RuntimeError as error:
        raise ValueError("controller_handoff_untrusted") from error


def _record_execution_manifest(manifest: Mapping[str, object]) -> None:
    """Copy this run's handoff into its supervisor-owned evidence directory."""
    raw = os.environ.get("OA_CONTROLLER_EXECUTION_MANIFEST_FILE")
    if not raw:
        return
    path = Path(raw)
    base = manifest_file().parent / "executions"
    if (not path.is_absolute() or path.parent.parent != base
            or _RUN_ID.fullmatch(path.parent.name) is None
            or path.name != "controller-manifest.json"):
        raise ValueError("controller_execution_evidence_failed")
    runtime_control.write_control_bytes_atomic(
        path, (json.dumps(dict(manifest), sort_keys=True) + "\n").encode(),
    )


def begin(config: Mapping[str, object]) -> dict:
    owner_pid = runtime_control.controller_owner_pid()
    writer_inactive = (
        not runtime_control.writer_active()
        and runtime_control.active_request_writers() == 0
    )
    if (
        not runtime_control.controller_active()
        or owner_pid != os.getpid()
        or not writer_inactive
    ):
        raise ValueError("controller_handoff_untrusted")
    previous = _read_manifest()
    if previous is not None and (
        previous["state"] not in {"completed", "failed"}
        or previous["children_reaped"] is not True
        or previous["controller_instance_id"] == CONTROLLER_INSTANCE_ID
    ):
        raise ValueError("controller_handoff_untrusted")
    revision, dirty = _code_state()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_controller_run_manifest",
        "state": "running",
        "run_id": secrets.token_hex(16),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "controller_pid": os.getpid(),
        "controller_instance_id": CONTROLLER_INSTANCE_ID,
        "previous_run_id": previous["run_id"] if previous else None,
        "previous_controller_instance_id": (
            previous["controller_instance_id"] if previous else None
        ),
        "code_revision": revision,
        "code_dirty": dirty,
        "config_fingerprint": _config_fingerprint(config),
        **({"review_configuration": config["review_configuration"]} if "review_configuration" in config else {}),
        "restart_mode": "fresh_process",
        "controller_lock_verified": True,
        "writer_inactive_at_start": True,
        "children_reaped": False,
        "restart_verified": True,
        "health": "unknown",
    }
    _record_execution_manifest(manifest)
    _write_manifest(manifest)
    return manifest


def finish(
    manifest: Mapping[str, object],
    *,
    status: str,
    children_reaped: bool,
    health: str,
) -> dict:
    if status not in {"completed", "failed"} or health not in _HEALTH:
        raise ValueError("controller_handoff_untrusted")
    current = _read_manifest()
    if current != dict(manifest):
        raise ValueError("controller_handoff_untrusted")
    terminal = {
        **current,
        "state": status,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "children_reaped": children_reaped,
        "health": health,
    }
    _write_manifest(terminal)
    _record_execution_manifest(terminal)
    return {
        "status": terminal["state"],
        "code_revision": terminal["code_revision"],
        "code_dirty": terminal["code_dirty"],
        "config_fingerprint": terminal["config_fingerprint"],
        "restart_mode": terminal["restart_mode"],
        "controller_lock_verified": terminal["controller_lock_verified"],
        "writer_inactive_at_start": terminal["writer_inactive_at_start"],
        "children_reaped": terminal["children_reaped"],
        "restart_verified": terminal["restart_verified"],
        "health": terminal["health"],
    }


def status() -> dict:
    controller_active = runtime_control.controller_active()
    writer_active = runtime_control.writer_active()
    active_request_writers = runtime_control.active_request_writers()
    try:
        manifest = _read_manifest()
    except ValueError:
        manifest = None
        manifest_valid = False
    else:
        manifest_valid = True
    if manifest is None:
        ready = (
            manifest_valid
            and not controller_active
            and not writer_active
            and active_request_writers == 0
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "preaudit_controller_handoff_status",
            "status": "ready" if ready else "blocked",
            "ready_for_start": ready,
            "controller_active": controller_active,
            "writer_active": writer_active,
            "active_request_writers": active_request_writers,
            "automation_state": "not_checked",
            "manifest_state": "not_recorded" if manifest_valid else "invalid",
            "lock_owner_consistent": not controller_active,
            "code_matches_current": "unknown",
            "code_revision": "unknown",
            "config_fingerprint": "unknown",
            "restart_mode": "fresh_process",
            "children_reaped": "unknown",
            "restart_verified": "unknown",
            "health": "unknown",
            "health_scope": "run_result",
            "monitoring_action": "check_start_gates" if ready else "investigate_handoff",
        }
    revision, dirty = _code_state()
    owner = runtime_control.controller_owner_pid() if controller_active else None
    lock_owner_consistent = (
        owner == manifest["controller_pid"]
        if controller_active
        else manifest["state"] != "running"
    )
    ready = (
        manifest["state"] in {"completed", "failed"}
        and manifest["children_reaped"] is True
        and manifest["controller_instance_id"] != CONTROLLER_INSTANCE_ID
        and not controller_active
        and not writer_active
        and active_request_writers == 0
        and lock_owner_consistent
    )
    active_consistent = (
        controller_active and lock_owner_consistent and manifest["state"] == "running"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_controller_handoff_status",
        "status": "active" if active_consistent else "ready" if ready else "blocked",
        "ready_for_start": ready,
        "controller_active": controller_active,
        "writer_active": writer_active,
        "active_request_writers": active_request_writers,
        "automation_state": "not_checked",
        "manifest_state": manifest["state"],
        "lock_owner_consistent": lock_owner_consistent,
        "code_matches_current": (
            _REVISION.fullmatch(revision) is not None
            and isinstance(dirty, bool)
            and revision == manifest["code_revision"]
            and dirty == manifest["code_dirty"]
        ),
        "code_revision": manifest["code_revision"],
        "config_fingerprint": manifest["config_fingerprint"],
        "restart_mode": manifest["restart_mode"],
        "children_reaped": manifest["children_reaped"],
        "restart_verified": manifest["restart_verified"],
        "health": manifest["health"],
        "health_scope": "run_result",
        "monitoring_action": (
            "wait_for_current_run" if active_consistent else
            "check_start_gates" if ready else "investigate_handoff"
        ),
    }
