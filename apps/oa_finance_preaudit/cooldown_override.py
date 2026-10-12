"""Audited cooldown timing exemption for one owned supervised invocation."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import UUID

from apps.oa_approval import runtime_control
from . import approval_guard as guard, maintenance_runtime, production_config, production_release

MANIFEST_ENV = "OA_CONTROLLER_EXECUTION_MANIFEST_FILE"


def validate_arguments(arguments: list[str]) -> None:
    expected = ["--rolling", "--fixed-capacity", "--capacity", "3", "--max-capacity", "3",
                "--confirm-write", "YES", "--recover-confirmed-no-write-once", "YES"]
    for workflow in production_config.FINANCE_WORKFLOWS:
        expected.extend(("--workflow", workflow))
    if arguments != expected:
        raise ValueError("approval_cooldown_override_scope_invalid")


def _write_new(path: Path, raw: bytes) -> None:
    with runtime_control.pinned_directory(path.parent, create=True) as parent:
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.fsync(parent)


def matches(value: dict) -> bool:
    raw_path = os.environ.get(guard.COOLDOWN_OVERRIDE_ENV)
    if raw_path is None:
        return False
    path = Path(raw_path)
    if (not path.is_absolute() or path.parent != guard.directory() / "cooldown-overrides"
            or len(path.stem) != 32 or path.suffix != ".json"):
        raise ValueError("approval_cooldown_override_untrusted")
    UUID(hex=path.stem)
    audit = guard.read(path)
    if (audit is None or audit.get("kind") != "approval_one_round_cooldown_override"
            or audit.get("schema_version") != 1 or audit.get("invocation_id") != path.stem
            or audit.get("owner_task_id") != os.environ.get(guard.OWNER_ENV)
            or audit.get("manifest_file") != os.environ.get(MANIFEST_ENV)
            or audit.get("manifest_file") != str(guard.directory().parent / "executions" / path.stem / "controller-manifest.json")
            or not isinstance(audit.get("owners"), list) or len(audit["owners"]) != 2
            or any(not guard._valid_identity(owner) for owner in audit["owners"])):
        raise ValueError("approval_cooldown_override_untrusted")
    if any(guard.process_identity(owner["pid"]) != owner for owner in audit["owners"]):
        raise ValueError("approval_cooldown_override_owner_lost")
    execution_directory = Path(audit["manifest_file"]).parent
    caller = guard.process_identity(os.getpid())
    if not execution_directory.exists():
        # Bootstrap is only for the original wrapper; mkdir(O_EXCL) permanently consumes it.
        if caller != audit["owners"][0]:
            raise ValueError("approval_cooldown_override_process_untrusted")
    else:
        execution = guard.read(execution_directory / "execution.json")
        audit_raw = runtime_control.read_control_bytes(path)
        if (execution is None or execution.get("kind") != "preaudit_controller_execution"
                or execution.get("invocation_id") != path.stem
                or execution.get("owner_task_id") != audit["owner_task_id"]
                or execution.get("state") not in {"starting", "waiting"}
                or execution.get("owner_parent_pid") != audit["owners"][0]["pid"]
                or execution.get("cooldown_override") != {"path": str(path), "sha256": hashlib.sha256(audit_raw).hexdigest()}
                or not guard._valid_identity(execution.get("supervisor_process"))
                or guard.process_identity(execution["supervisor_process"]["pid"]) != execution["supervisor_process"]):
            raise ValueError("approval_cooldown_override_consumed")
        supervisor = execution["supervisor_process"]
        if caller != supervisor:
            controller = execution.get("controller_process")
            if controller is None and execution["state"] == "starting":
                # Only the supervisor's direct child may cross the Popen/receipt publication window.
                if os.getppid() != supervisor["pid"]:
                    raise ValueError("approval_cooldown_override_process_untrusted")
            else:
                if (not guard._valid_identity(controller)
                        or controller["pid"] != execution.get("controller_pid")
                        or guard.process_identity(controller["pid"]) != controller):
                    raise ValueError("approval_cooldown_override_process_untrusted")
                manifest = guard.read(Path(audit["manifest_file"]))
                if caller == controller and os.environ.get(guard.RUN_ENV) is None:
                    if manifest is not None:
                        raise ValueError("approval_cooldown_override_consumed")
                else:
                    if (manifest is None or manifest.get("state") != "running"
                            or manifest.get("controller_pid") != controller["pid"]
                            or manifest.get("run_id") != os.environ.get(guard.RUN_ENV)
                            or manifest.get("controller_instance_id") != os.environ.get(guard.INSTANCE_ENV)):
                        raise ValueError("approval_cooldown_override_run_untrusted")
                    if caller != controller:
                        worker = guard.read(guard.directory() / "runs" / manifest["run_id"] / f"worker-{os.getpid()}.json")
                        if (worker is None or worker.get("kind") != "approval_readonly_worker"
                                or worker.get("role") not in {"prepare", "review"}
                                or worker.get("process") != caller or worker.get("controller") != controller
                                or worker.get("run_id") != manifest["run_id"]
                                or worker.get("controller_instance_id") != manifest["controller_instance_id"]):
                            raise ValueError("approval_cooldown_override_worker_untrusted")
    pinned = production_release._pinned_control(guard.directory() / "cooldown.json")
    return (audit.get("cooldown") == value
            and audit.get("cooldown_identity") == list(pinned[0])
            and audit.get("cooldown_sha256") == hashlib.sha256(pinned[1]).hexdigest())


@contextmanager
def one_round(project_root: Path, *, owner_task_id: str, invocation_id: str,
              original: maintenance_runtime.OriginalExecution):
    UUID(owner_task_id)
    UUID(hex=invocation_id)
    if guard.COOLDOWN_OVERRIDE_ENV in os.environ:
        raise ValueError("approval_cooldown_override_inherited")
    root = project_root.resolve()
    maintenance_runtime.assert_target_environment(root)
    revision = production_release._git(root, "rev-parse", "HEAD")
    production_release._source(root, revision, production_release.ORIGIN, synchronized=True)
    if production_config.repository_findings(root):
        raise RuntimeError("approval_cooldown_override_configuration_invalid")
    owners = [guard.process_identity(os.getpid()), guard.process_identity(os.getppid())]
    if any(owner is None for owner in owners):
        raise ValueError("approval_execution_owner_untrusted")
    # Observe new official errors before binding the exact historical incident.
    guard.observe_project_errors(root)
    stop_path, cooldown_path = runtime_control.stop_file(), guard.directory() / "cooldown.json"
    policy = root / "runtime/control/write-policy.json"
    with maintenance_runtime.target_runtime_leases(root):
        stop = production_release._pinned_control(stop_path)
        cooldown = production_release._pinned_control(cooldown_path)
        policy_before = production_release._pinned_control(policy, private=False)
        if (stop[1] != f"stop_requested:{guard.STOP_REASON}\n".encode()
                or runtime_control.stop_reason() != guard.STOP_REASON):
            raise ValueError("approval_cooldown_override_stop_invalid")
        value = guard.cooldown()
        manifest = runtime_control.read_control_bytes(root / "runtime/control/rolling-controller-manifest.json")
        report = maintenance_runtime.read_original_execution(root, original, official_stop_source_only=True)
        maintenance_runtime.assert_original_execution_safe(root, original, report, manifest, official_stop_source_only=True)
        snapshot = maintenance_runtime.read_live_status(root, require_reaped=True)
        maintenance_runtime.assert_live_safe(snapshot)
        # Re-read all original receipts and readonly ownership after the live query.
        if (report != maintenance_runtime.read_original_execution(root, original, official_stop_source_only=True)
                or stop != production_release._pinned_control(stop_path)
                or cooldown != production_release._pinned_control(cooldown_path)
                or policy_before != production_release._pinned_control(policy, private=False)
                or any(guard.process_identity(owner["pid"]) != owner for owner in owners)):
            raise ValueError("approval_cooldown_override_evidence_changed")
        production_release._source(root, revision, production_release.ORIGIN, synchronized=True)
        archive = guard.directory() / "cooldown-overrides" / f"{invocation_id}-cooldown.json"
        audit_path = archive.with_name(f"{invocation_id}.json")
        audit = {"schema_version": 1, "kind": "approval_one_round_cooldown_override",
                 "owner_task_id": owner_task_id, "invocation_id": invocation_id,
                 "manifest_file": str(guard.directory().parent / "executions" / invocation_id / "controller-manifest.json"),
                 "project_root": str(root), "code_revision": revision, "authorized_at": time.time(), "owners": owners,
                 "cooldown": value, "cooldown_identity": list(cooldown[0]),
                 "cooldown_sha256": hashlib.sha256(cooldown[1]).hexdigest(), "cooldown_archive": str(archive),
                 "stop_identity": list(stop[0]), "stop_sha256": hashlib.sha256(stop[1]).hexdigest(),
                 "stop_payload": stop[1].decode("ascii"), "original_execution": report,
                 "startup_status_file": snapshot["maintenance_status_file"],
                 "startup_current_inputs": snapshot["maintenance_current_inputs"], "write_state": "not_requested"}
        _write_new(archive, cooldown[1])
        _write_new(audit_path, (json.dumps(audit, sort_keys=True) + "\n").encode())
        with runtime_control.stop_control_lease():
            if (stop != production_release._pinned_control(stop_path)
                    or cooldown != production_release._pinned_control(cooldown_path)
                    or not runtime_control._clear_stop_locked(stop[1])):
                raise ValueError("approval_cooldown_override_clear_failed")
    previous = {key: os.environ.get(key) for key in (guard.COOLDOWN_OVERRIDE_ENV, guard.OWNER_ENV, MANIFEST_ENV)}
    try:
        os.environ[guard.COOLDOWN_OVERRIDE_ENV] = str(audit_path)
        os.environ[guard.OWNER_ENV] = owner_task_id
        os.environ[MANIFEST_ENV] = audit["manifest_file"]
        print(json.dumps({"kind": audit["kind"], "invocation_id": invocation_id, "owner_task_id": owner_task_id,
                          "audit_file": str(audit_path), "stop_cleared": True,
                          "startup_status_file": audit["startup_status_file"], "write_state": "not_requested"}), flush=True)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
