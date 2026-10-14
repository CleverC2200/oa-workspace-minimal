"""Fresh-run checks and readonly process ownership, separate from the Writer."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import time
from uuid import uuid4

from apps.oa_approval import runtime_control
from . import approval_guard as guard, controller_handoff

HEARTBEAT_MAX_AGE = 45


def run_directory(run_id: str) -> Path:
    if controller_handoff._RUN_ID.fullmatch(run_id) is None:
        raise ValueError("approval_run_untrusted")
    return guard.directory() / "runs" / run_id


def context() -> tuple[str, str, str] | None:
    run, instance = os.environ.get(guard.RUN_ENV), os.environ.get(guard.INSTANCE_ENV)
    if run is None and instance is None:
        return None
    if not run or not instance or any(controller_handoff._RUN_ID.fullmatch(value) is None for value in (run, instance)):
        raise ValueError("approval_run_untrusted")
    return run, instance, str(guard.directory())


@contextmanager
def run_scope(manifest: dict, project_root: Path):
    previous = {key: os.environ.get(key) for key in (guard.RUN_ENV, guard.INSTANCE_ENV)}
    identity = guard.process_identity(os.getpid())
    if identity is None:
        raise ValueError("approval_run_untrusted")
    path = run_directory(manifest["run_id"]) / "progress.json"
    guard.write(path, {"schema_version": 1, "kind": "approval_run_progress",
                       "run_id": manifest["run_id"], "controller_instance_id": manifest["controller_instance_id"],
                       "controller": identity, "project_root": str(project_root.resolve()),
                       "heartbeat_at": time.time(), "actions": [], "phase_deadlines": [], "writer_active": False})
    try:
        os.environ[guard.RUN_ENV] = manifest["run_id"]
        os.environ[guard.INSTANCE_ENV] = manifest["controller_instance_id"]
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def register_readonly_worker(role: str, binding: tuple[str, str, str] | None) -> dict | None:
    if binding is None:
        return None
    run, instance, directory = binding
    if (role not in {"review", "prepare"} or directory != str(guard.directory())
            or controller_handoff._RUN_ID.fullmatch(instance) is None):
        raise ValueError("approval_worker_untrusted")
    identity, parent = guard.process_identity(os.getpid()), guard.process_identity(os.getppid())
    if identity is None or parent is None or identity["pgid"] != identity["pid"]:
        raise ValueError("approval_worker_untrusted")
    value = {"schema_version": 1, "kind": "approval_readonly_worker", "role": role,
             "run_id": run, "controller_instance_id": instance, "process": identity, "controller": parent}
    guard.write(run_directory(run) / f"worker-{identity['pid']}.json", value)
    return value


def reap_worker_models(record: dict | None) -> None:
    if record is None:
        return
    # Serialize with Popen registration before the watcher kills its own group.
    with runtime_control.stop_control_lease():
        runtime_control._write_stop_locked("controller_cycle_stop")
    guard.reap_models(run_id=record["run_id"], worker=record["process"], orphaned=True)


def submit_readonly(executor, function, *args, role: str):
    binding = context()
    if binding is None:
        return executor.submit(function, *args)
    run, instance, _directory = binding
    pool = getattr(executor, "_approval_pool_id", None)
    if pool is None:
        pool = uuid4().hex
        executor._approval_pool_id = pool
    path = run_directory(run) / f"pool-{pool}.json"
    progress = guard.read(run_directory(run) / "progress.json")
    if progress is None or progress.get("controller") != guard.process_identity(os.getpid()):
        raise ValueError("approval_pool_untrusted")
    value = {"schema_version": 1, "kind": "approval_readonly_pool", "run_id": run,
             "controller_instance_id": instance, "controller": progress["controller"],
             "pool_id": pool, "role": role, "state": "launching", "processes": []}
    # Persist intent before ProcessPool can create a child. A crash in this window
    # remains unknown, even when no initializer registration has appeared yet.
    guard.write(path, value)
    future = executor.submit(function, *args)
    processes = getattr(executor, "_processes", None)
    if not isinstance(processes, dict) or not processes:
        raise ValueError("approval_pool_untrusted")
    identities = []
    for process in processes.values():
        identity = guard.process_identity(process.pid)
        if identity is None:
            raise ValueError("approval_pool_untrusted")
        # setsid can still be pending; only its future PID-named group is owned.
        identities.append({**identity, "pgid": identity["pid"]})
    guard.write(path, {**value, "state": "registered", "processes": identities})
    return future


def _pool_processes(manifest: dict, progress: dict) -> list[dict]:
    identities = []
    for path in run_directory(manifest["run_id"]).glob("pool-*.json"):
        value = guard.read(path)
        if (value is None or value.get("kind") != "approval_readonly_pool" or value.get("schema_version") != 1
                or value.get("run_id") != manifest["run_id"] or value.get("controller_instance_id") != manifest["controller_instance_id"]
                or value.get("controller") != progress["controller"] or value.get("role") not in {"review", "prepare"}
                or value.get("state") != "registered" or not isinstance(value.get("processes"), list) or not value["processes"]
                or not isinstance(value.get("pool_id"), str) or controller_handoff._RUN_ID.fullmatch(value["pool_id"]) is None
                or path.name != f"pool-{value['pool_id']}.json"
                or any(not guard._valid_identity(identity) or identity["pgid"] != identity["pid"] for identity in value["processes"])):
            raise ValueError("approval_pool_children_unknown")
        identities.extend(value["processes"])
    return identities


def _progress(manifest: dict) -> dict | None:
    value = guard.read(run_directory(manifest["run_id"]) / "progress.json")
    if value is None:
        return None
    if (value.get("kind") != "approval_run_progress" or value.get("schema_version") != 1
            or value.get("run_id") != manifest["run_id"]
            or value.get("controller_instance_id") != manifest["controller_instance_id"]
            or not guard._valid_identity(value.get("controller"))
            or value["controller"]["pid"] != manifest["controller_pid"]
            or not guard._timestamp(value.get("heartbeat_at"))
            or not isinstance(value.get("actions"), list) or not isinstance(value.get("phase_deadlines"), list)
            or type(value.get("writer_active")) is not bool):
        raise ValueError("approval_run_untrusted")
    root = Path(value["project_root"]) / "runtime" / "finance_preaudit_flows"
    for action in value["actions"]:
        path = Path(action)
        if not path.is_absolute() or not path.is_relative_to(root) or path.name != "action.json":
            raise ValueError("approval_run_untrusted")
    if any(not guard._timestamp(deadline) for deadline in value["phase_deadlines"]):
        raise ValueError("approval_run_untrusted")
    return value


def heartbeat(run_id: str | None, instance: str | None, actions: set[Path], deadlines: list[float], *, writer_active: bool) -> None:
    if run_id is None:
        return
    path = run_directory(run_id) / "progress.json"
    value = guard.read(path)
    if (value is None or value.get("controller_instance_id") != instance
            or value.get("controller", {}).get("pid") != os.getpid()):
        raise ValueError("approval_run_untrusted")
    now = time.time()
    if now - value.get("execution_owner_checked_at", 0) >= guard.OBSERVATION_SECONDS:
        if not guard.execution_owners_alive():
            runtime_control.request_stop("codex_review_technical_failure")
        manifest = controller_handoff._read_manifest()
        if (manifest is not None and manifest["state"] == "running"
                and manifest.get("review_configuration", {}).get("transport") == "host_chat"):
            from . import host_chat_review
            try:
                owner = os.environ.get(guard.OWNER_ENV)
                if owner is None:
                    raise ValueError("host_review_parent_untrusted")
                host_chat_review.assert_round_alive(owner=owner, manifest=manifest)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                # Close admission through the existing stop gate; the Writer must finish normally.
                runtime_control.request_cycle_stop()
        value["execution_owner_checked_at"] = now
    guard.write(path, {**value, "heartbeat_at": time.time(), "actions": sorted({*value["actions"], *(str(path) for path in actions)}),
                       "phase_deadlines": deadlines, "writer_active": writer_active})


def _workers(manifest: dict, progress: dict) -> list[dict]:
    root = run_directory(manifest["run_id"])
    if root.is_symlink():
        raise ValueError("approval_worker_untrusted")
    records = []
    for path in root.glob("worker-*.json"):
        value = guard.read(path)
        if (value is None or value.get("kind") != "approval_readonly_worker" or value.get("schema_version") != 1
                or value.get("role") not in {"review", "prepare"} or value.get("run_id") != manifest["run_id"]
                or value.get("controller_instance_id") != manifest["controller_instance_id"]
                or value.get("controller") != progress["controller"] or not guard._valid_identity(value.get("process"))
                or value["process"]["pgid"] != value["process"]["pid"]
                or path.name != f"worker-{value['process']['pid']}.json"):
            raise ValueError("approval_worker_untrusted")
        records.append(value)
    return records


def readonly_reaped(manifest: dict) -> bool:
    progress = _progress(manifest)
    if progress is None:
        return False
    return guard.models_reaped(manifest["run_id"]) and all(
        guard.process_identity(identity["pid"]) is None and not guard.group_alive(identity["pgid"])
        for identity in [*_pool_processes(manifest, progress), *(record["process"] for record in _workers(manifest, progress))])


def certify_interruption(manifest: dict, *, cause: str) -> dict:
    if (cause not in {guard.STOP_REASON, "controller_orphaned", "readonly_deadline_exceeded"}
            or runtime_control.controller_owner_pid() != os.getpid()
            or not guard.maintenance_writer_idle()
            or not readonly_reaped(manifest)):
        raise ValueError("approval_interruption_untrusted")
    progress = _progress(manifest)
    value = {"schema_version": 1, "kind": "approval_closed_readonly_run", "run_id": manifest["run_id"],
             "controller_instance_id": manifest["controller_instance_id"], "controller": progress["controller"],
             "project_root": progress["project_root"], "actions": progress["actions"], "closed_at": time.time(),
             "readonly_reaped": True, "writer_inactive": True, "cause": cause,
             "cooldown": guard.cooldown() if cause == guard.STOP_REASON else None}
    path = run_directory(manifest["run_id"]) / "closed-readonly.json"
    existing = guard.read(path)
    if existing:
        if any(existing.get(key) != value[key] for key in value if key not in {"closed_at", "cooldown"}):
            raise ValueError("approval_interruption_untrusted")
        return existing
    guard.write(path, value)
    return value


def interrupt_unwritten(manifest: dict, *, cause: str) -> int:
    from .flow_engine import FlowEngine
    with runtime_control.request_writer_lease(runtime_control.GLOBAL_WRITER_KEY, runtime_control.writer_lock_file(), blocking=False):
        proof = certify_interruption(manifest, cause=cause)
        engine = FlowEngine(root=Path(proof["project_root"]) / "runtime" / "finance_preaudit_flows",
                            discover_candidates=lambda _: (), collect_material=lambda *_: {})
        count = 0
        for action in proof["actions"]:
            if engine.interrupt_unwritten_review(Path(action), run_id=manifest["run_id"]):
                count += 1
        return count


def startup(project_root: Path, *, observe_errors: bool = True) -> dict:
    """No model/OA requests. Unknown ownership blocks, never authorizes killing."""
    if guard.COOLDOWN_OVERRIDE_ENV in os.environ:
        guard.cooldown_ignored(guard.cooldown())
    if observe_errors:
        guard.observe_project_errors(project_root)
    manifest = controller_handoff._read_manifest()
    value = guard.cooldown()
    active = runtime_control.controller_active()
    progress = _progress(manifest) if manifest else None
    if progress and Path(progress["project_root"]).resolve() != Path(project_root).resolve():
        return {"status": "blocked", "reason": "previous_run_project_changed"}
    cleanup = False
    if active:
        if manifest is None or progress is None or manifest["state"] != "running":
            return {"status": "blocked", "reason": "previous_run_ownership_unknown"}
        if (runtime_control.controller_owner_pid() != manifest["controller_pid"]
                or guard.process_identity(manifest["controller_pid"]) != progress["controller"]):
            return {"status": "blocked", "reason": "previous_run_identity_changed"}
        now = time.time()
        if runtime_control.stop_requested():
            return {"status": "blocked", "reason": "existing_stop_preserved", "run_id": manifest["run_id"]}
        if not 0 <= now - progress["heartbeat_at"] <= HEARTBEAT_MAX_AGE:
            return {"status": "blocked", "reason": "previous_run_progress_unknown"}
        expired = any(deadline < now for deadline in progress["phase_deadlines"])
        if expired:
            runtime_control.request_cycle_stop()
            guard.reap_models(run_id=manifest["run_id"])
            return {"status": "blocked", "reason": "previous_run_deadline_exceeded", "cleanup": "readonly_only"}
        return {"status": "skipped", "reason": "previous_run_busy", "run_id": manifest["run_id"]}
    with runtime_control.global_controller_lease():
        if runtime_control.writer_active() or runtime_control.active_request_writers():
            return {"status": "blocked", "reason": "writer_in_progress"}
        if manifest and (manifest["state"] == "running" or manifest["children_reaped"] is not True
                         or (progress is not None and not readonly_reaped(manifest))):
            if progress is None or guard.process_identity(manifest["controller_pid"]) == progress["controller"]:
                return {"status": "blocked", "reason": "previous_run_ownership_unknown"}
            if not guard.reap_models(run_id=manifest["run_id"], orphaned=True):
                return {"status": "blocked", "reason": "approval_model_children_unknown"}
            for identity in [*_pool_processes(manifest, progress), *(record["process"] for record in _workers(manifest, progress))]:
                if not guard._terminate_registered(identity):
                    return {"status": "blocked", "reason": "approval_worker_children_unknown"}
            cause = guard.STOP_REASON if runtime_control.stop_reason() == guard.STOP_REASON else "controller_orphaned"
            interrupt_unwritten(manifest, cause=cause)
            original = controller_handoff.manifest_file().with_name(f"orphan-controller-{manifest['run_id']}.json")
            if guard.read(original) not in (None, manifest):
                raise ValueError("approval_run_untrusted")
            guard.write(original, manifest)
            controller_handoff.finish(manifest, status="failed", children_reaped=True, health="degraded")
            cleanup = True
            if runtime_control.stop_reason() == "controller_cycle_stop":
                runtime_control.clear_cycle_stop()
        if not guard.reap_models():
            return {"status": "blocked", "reason": "approval_model_children_unknown"}
        if value and time.time() < value["blocked_until"] and not guard.cooldown_ignored(value):
            return {"status": "skipped", "reason": "approval_model_cooldown", "blocked_until": value["blocked_until"], "cleanup": cleanup}
        if runtime_control.stop_reason() == guard.STOP_REASON:
            if not guard.clear_expired_cooldown():
                return {"status": "blocked", "reason": "approval_cooldown_release_untrusted"}
        if runtime_control.stop_requested():
            return {"status": "blocked", "reason": "existing_stop_preserved"}
        controller_handoff.assert_ready_for_start()
    return {"status": "ready", "cleanup": cleanup}
