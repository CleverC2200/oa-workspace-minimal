"""Recover an historical postcheck stop from a reviewed candidate, without OA writes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets

from apps.oa_approval import runtime_control
from . import maintenance_runtime, production_release, controller_handoff, stop_recovery

BLOCKED = "writer_stop_maintenance_blocked"
_REASON = "writer_result_unknown"

# Run only installed validators in the target interpreter. No candidate imports,
# authentication output, model calls, state changes, or OA submission are involved.
_BOUND_READER = r"""
import hashlib,json,os,sys
from pathlib import Path
from apps.oa_finance_preaudit import reconciliation,production_write,flow_engine,approval_lifecycle,controller_handoff
root=Path.cwd()/"runtime/finance_preaudit_flows"
action=Path(sys.argv[1]);attempt=action.parent
if action != action.resolve() or action.name != "action.json" or not action.is_relative_to(root):
    raise ValueError("bound_action_untrusted")
manifest=controller_handoff._read_manifest()
progress=approval_lifecycle._progress(manifest) if manifest else None
if not progress or str(action) not in progress["actions"]:
    raise ValueError("bound_run_untrusted")
states=[s for s in reconciliation.latest_states(root) if s.get("action_file")==str(action)]
if len(states)!=1: raise ValueError("bound_state_untrusted")
state=states[0]
if (state.get("execution_state")!="needs_admin" or state.get("write_state")!="unknown"
    or state.get("reason")!="writer_postcheck_timeout"
    or state.get("state_file")!=str(attempt/"state.json")
    or os.path.lexists(attempt/"execution-receipt.json")):
    raise ValueError("bound_state_untrusted")
flow_engine.validate_frozen_review_inputs(attempt_root=attempt,state=state,workflow_key=state["workflow_key"])
if not production_write.verified_unknown_write_failure(attempt,state,state["reason"]):
    raise ValueError("bound_boundary_untrusted")
files={}
for name in ("action.json","state.json","material.json","review-packet.json","writer-boundary.json",
             "review-decision.json","remark.txt","submission-envelope.json","manual-review-receipt.json"):
    p=attempt/name
    if name=="manual-review-receipt.json" and not p.exists(): continue
    s=p.lstat()
    if p.is_symlink() or not p.is_file() or s.st_uid!=os.getuid() or s.st_mode & 0o077:
        raise ValueError("bound_file_untrusted")
    raw=p.read_bytes();after=p.lstat()
    keys=("st_dev","st_ino","st_mtime_ns","st_ctime_ns","st_uid","st_mode","st_size")
    identity=[getattr(s,k) for k in keys]
    if identity!=[getattr(after,k) for k in keys]: raise ValueError("bound_file_changed")
    files[name]={"identity":identity,"sha256":hashlib.sha256(raw).hexdigest()}
keys=("workflow_key","case_key","attempt","request_id","received_at","action_file","state_file",
      "execution_state","write_state","reason")
print(json.dumps({"state":{k:state[k] for k in keys},"files":files,
    "run_id":manifest["run_id"],"controller_instance_id":manifest["controller_instance_id"]}))
"""


def _read_bound(target: Path, action: Path) -> dict:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    result = maintenance_runtime._run_readonly(
        [
            str(target / ".venv/bin/python"),
            "-B",
            "-E",
            "-c",
            _BOUND_READER,
            str(action),
        ],
        target_root=target,
        environment=environment,
        timeout=60,
        require_reaped=True,
    )
    if result.returncode or len(result.stdout.encode()) > 100_000:
        raise ValueError(BLOCKED)
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or set(value) != {
        "state",
        "files",
        "run_id",
        "controller_instance_id",
    }:
        raise ValueError(BLOCKED)
    return value


def _controls(
    target: Path, automation: production_release.ProductionAutomation
) -> tuple:
    if os.environ.get("OA_FINANCE_PREAUDIT_STOP_REQUESTED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }:
        raise ValueError(BLOCKED)
    stop = production_release._pinned_control(target / "runtime/stop-preaudit.flag")
    if stop[1] != b"stop_requested:writer_result_unknown\n":
        raise ValueError(BLOCKED)
    return stop, production_release._paused_cron(automation)


def recover(
    *,
    target_root: Path,
    action_file: Path,
    expected_revision: str,
    original: maintenance_runtime.OriginalExecution,
    automation: production_release.ProductionAutomation,
    confirmed: bool,
) -> dict:
    """Clear only this resolved stop; keep the original unknown attempt unchanged."""
    if confirmed is not True:
        raise ValueError(BLOCKED)
    target = Path(target_root).expanduser().absolute()
    maintenance_runtime.assert_target_environment(target)
    candidate = Path(__file__).resolve().parents[2]
    git = production_release._git
    if (
        candidate == target
        or git(candidate, "rev-parse", "HEAD") != expected_revision
        or git(candidate, "rev-parse", "origin/main") != expected_revision
        or git(candidate, "status", "--porcelain")
    ):
        raise ValueError(BLOCKED)
    production_release._source(target, expected_revision, production_release.ORIGIN)
    marker = target / "runtime/stop-preaudit.flag"
    receipt_file = (
        target
        / "runtime/control"
        / f"writer-stop-maintenance-{secrets.token_hex(16)}.json"
    )
    cleared = False
    clear_started = False
    try:
        with maintenance_runtime.target_runtime_leases(target):
            controls = _controls(target, automation)
            manifest_path = target / "runtime/control/rolling-controller-manifest.json"
            manifest = controller_handoff.manifest_snapshot(manifest_path)
            report = maintenance_runtime.read_original_execution(target, original)
            maintenance_runtime.assert_original_execution_safe(
                target, original, report, manifest
            )
            bound = _read_bound(target, action_file)
            if any(
                bound[key] != report[key]
                for key in ("run_id", "controller_instance_id")
            ):
                raise ValueError(BLOCKED)
            snapshot = maintenance_runtime.read_live_status(target, require_reaped=True)
            maintenance_runtime.assert_live_safe(snapshot)
            state = bound["state"]
            stop_recovery._require_historical(snapshot["reconciliation"], state)
            if automation.findings(target):
                raise ValueError(BLOCKED)
            receipt = {
                "kind": "preaudit_writer_stop_maintenance",
                "schema_version": 1,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "phase": "validated",
                "target_root": str(target),
                "candidate_revision": expected_revision,
                "original_execution": report,
                "bound_evidence": bound,
                "current_status_file": snapshot["maintenance_status_file"],
                "current_input_proof": snapshot["maintenance_current_inputs"],
                "stop_sha256": hashlib.sha256(controls[0][1]).hexdigest(),
                "stop_cleared": False,
                "bound_write_state": "unknown",
                "write_state": "not_requested",
            }

            def record() -> None:
                runtime_control.write_control_bytes_atomic(
                    receipt_file, (json.dumps(receipt, sort_keys=True) + "\n").encode()
                )

            record()
            if (
                _controls(target, automation) != controls
                or _read_bound(target, action_file) != bound
                or controller_handoff.manifest_snapshot(manifest_path) != manifest
            ):
                raise ValueError(BLOCKED)
            production_release._source(
                target, expected_revision, production_release.ORIGIN
            )
            maintenance_runtime.assert_original_execution_safe(
                target,
                original,
                maintenance_runtime.read_original_execution(target, original),
                manifest,
            )
            if _controls(target, automation) != controls:
                raise ValueError(BLOCKED)
            clear_started = True
            identity = controls[0][0]
            cleared = runtime_control.clear_failure_stop(
                expected_reason=_REASON,
                expected_identity=(identity[0], identity[1], identity[2]),
                target=marker,
            )
            receipt.update(
                phase="cleared" if cleared else "clear_rejected", stop_cleared=cleared
            )
            record()
            return {
                "status": "recovered" if cleared else "blocked",
                "stop_cleared": cleared,
                "receipt_file": str(receipt_file),
                "bound_write_state": "unknown",
                "write_state": "not_requested",
            }
    except (OSError, RuntimeError, ValueError):
        if not clear_started:
            raise
        return {
            "status": "maintenance_completion_failed",
            "stop_cleared": not os.path.lexists(marker),
            "receipt_file": str(receipt_file),
            "write_state": "not_requested",
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--bind-action-file", type=Path, required=True)
    parser.add_argument("--expected-candidate-revision", required=True)
    parser.add_argument("--original-receipt-file", type=Path, required=True)
    parser.add_argument("--original-owner-task-id", required=True)
    parser.add_argument("--original-tool-session-id", required=True)
    parser.add_argument("--automation-file", type=Path, required=True)
    parser.add_argument("--production-project-id", required=True)
    parser.add_argument("--expected-schedule", required=True)
    parser.add_argument("--confirm-reconciled", choices=("YES",), required=True)
    args = parser.parse_args(argv)
    try:
        result = recover(
            target_root=args.target_root,
            action_file=args.bind_action_file,
            expected_revision=args.expected_candidate_revision,
            confirmed=args.confirm_reconciled == "YES",
            original=maintenance_runtime.OriginalExecution(
                args.original_receipt_file,
                args.original_owner_task_id,
                args.original_tool_session_id,
            ),
            automation=production_release.ProductionAutomation(
                args.automation_file, args.production_project_id, args.expected_schedule
            ),
        )
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        result = {
            "status": "blocked",
            "error_code": BLOCKED,
            "stop_cleared": False,
            "write_state": "not_requested",
        }
    print(json.dumps(result))
    return 0 if result["status"] == "recovered" else 1


if __name__ == "__main__":
    raise SystemExit(main())
