from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Callable, Sequence
from datetime import datetime, timezone

from apps.oa_approval import api as legacy_api
from apps.oa_approval import flow as legacy_flow
from apps.oa_approval import attachment_cache, runtime_control
from . import attempt_state
from .case_bundle import collect_case
from . import (
    controller_handoff,
    flow_engine,
    observability,
    production_pending,
    production_write,
    reconciliation,
    runtime_maintenance,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FlowEngineFactory = Callable[[], flow_engine.FlowEngine]
RuntimeOperation = Callable[[Path], dict]
LiveStatusOperation = Callable[[], dict]
ReconciliationOperation = Callable[[Path], dict]
InvocationSink = Callable[[dict], None]
_RUNTIME_FAILURE_CODES = {
    "retention_action_invalid",
    "retention_archive_conflict",
    "retention_path_outside_root",
    "retention_plan_invalid",
    "retention_root_invalid",
    "retention_root_unmarked",
    "runtime_now_invalid",
    "runtime_path_symlink",
    "runtime_root_invalid",
    "runtime_state_invalid",
    "runtime_live_configuration_missing",
    "runtime_live_check_failed",
    "runtime_reconciliation_invalid",
    "oa_execution_identity_login_failed",
    "oa_session_expired",
    "rpa_cdp_timeout",
    "rpa_cdp_unavailable",
    "rpa_session_invalid",
    "rpa_session_missing",
    "oa_auth_file_permissions_invalid",
    "oa_auth_file_outside_worktree_required",
    "cache_clean_failed",
    "cache_clean_runtime_active",
    "status_output_file_exists",
    "status_output_file_failed",
}
_FLOW_FAILURE_CODES = {
    "flow_candidate_invalid",
    "observability_write_failed",
    "persistent_state_invalid",
    "review_decision_conflict",
    "review_decision_invalid",
    "review_decision_unreadable",
    "recovery_confirmation_required",
    "recovery_live_state_conflict",
    "recovery_live_state_unavailable",
    "recovery_not_safe",
    "recovery_runtime_active",
    "run_mode_unauthorized",
    "production_rolling_controller_required",
    "write_authorization_invalid",
    "workflow_not_implemented",
}


def _json_default(value: object) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _print_payload(payload: dict) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        )
    )



def _status_file_payload(payload: dict, target: Path) -> dict:
    """Keep the full snapshot locally before emitting a small, unchanged gate view."""
    from .case_recovery import summary

    target = target.expanduser().absolute()
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      default=_json_default) + "\n").encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        descriptor = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w+b") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            if hashlib.sha256(stream.read()).hexdigest() != digest:
                raise OSError("status_output_readback_failed")
    except FileExistsError as error:
        raise RuntimeError("status_output_file_exists") from error
    except OSError as error:
        # Preserve any partial file as evidence; it is not a successful receipt.
        raise RuntimeError("status_output_file_failed") from error
    return {
        **summary(payload, detail_limit=0),
        "status_file": {"path": str(target), "bytes": len(raw), "sha256": digest},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oa-finance-preaudit")
    subparsers = parser.add_subparsers(dest="command", required=True)
    next_action = subparsers.add_parser("next-action")
    next_action.add_argument("--workflow", required=True)
    next_action.add_argument(
        "--mode",
        choices=("preview", "controlled-write"),
        default="preview",
    )
    next_action.add_argument("--confirm-write", choices=("YES",), default=None)
    next_action.add_argument("--recover-action-file", type=Path, default=None)
    next_action.add_argument("--confirm-recovery", choices=("YES",), default=None)
    next_action.add_argument(
        "--confirm-unknown-no-write",
        choices=("YES",),
        default=None,
    )
    next_action.add_argument("--confirm-unknown-request-id", default=None)
    complete_review = subparsers.add_parser("complete-review")
    complete_review.add_argument("--action-file", type=Path, required=True)
    complete_review.add_argument("--decision-file", type=Path, required=True)
    complete_review.add_argument("--confirm-write", choices=("YES",), default=None)
    status = subparsers.add_parser("status")
    status.add_argument("--root", type=Path, default=None)
    status.add_argument("--check-live", action="store_true")
    status.add_argument("--reconcile", action="store_true")
    status.add_argument("--summary", action="store_true")
    status.add_argument("--output-file", type=Path, default=None,
                        help="Save full JSON to a new private file; print gates and counts only")
    prune = subparsers.add_parser("prune")
    prune.add_argument("--root", type=Path, default=None)
    clean = subparsers.add_parser("clean")
    clean.add_argument("--all-cache", action="store_true", required=True)
    return parser


def _runtime_root(value: Path | None) -> Path:
    return Path(
        value
        or os.environ.get("OA_FINANCE_PREAUDIT_FLOW_ROOT")
        or PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    )


def _live_pending_identities() -> list[dict]:
    configs = legacy_flow.load_all_configs()
    oa_api = legacy_api.build_api(configs[0])
    return production_pending.identify_pending_rows(
        oa_api.pending_rows(page_size=200, max_pages=100),
        configs,
    )


def _live_flow_engine(
    *,
    controlled: bool = False,
    selected_identity: tuple[str, str] | None = None,
    target_case_keys: frozenset[str] | None = None,
) -> flow_engine.FlowEngine:
    configs = legacy_flow.load_all_configs()
    oa_api = legacy_api.build_api(configs[0])

    def discover(workflow_key: str) -> list[dict]:
        rows = oa_api.pending_rows(page_size=200, max_pages=100)
        identities = production_pending.identify_pending_rows(rows, configs)
        return [
            {
                "request_id": item["requestId"],
                "received_at": item["received_at"],
                "workflow_key": item["flow_key"],
            }
            for item in identities
            if item["support_status"] == "supported"
            and item["flow_key"] == workflow_key
            and (
                selected_identity is None
                or (item["requestId"], item["received_at"])
                == selected_identity
            )
        ]

    def collect(request_id: str, attempt_root: Path) -> dict:
        return collect_case(
            request_id,
            oa_api=oa_api,
            configs=configs,
            allow_browser_download=True,
            attachment_root=attempt_root / "attachments",
            prepared_root=attempt_root / "prepared",
            attachment_timeout_sec=180,
        )

    root = Path(
        os.environ.get("OA_FINANCE_PREAUDIT_FLOW_ROOT")
        or PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    )
    ports = production_write.build_ports(root=root) if controlled else {}
    return flow_engine.FlowEngine(
        root=root,
        discover_candidates=discover,
        discover_handled=lambda: oa_api.handled_rows(page_size=200, max_pages=100),
        collect_material=collect,
        **ports,
    )


def _live_reconciliation(
    root: Path,
    *,
    workflow_key: str | None = None,
    workflow_keys: frozenset[str] | None = None,
) -> dict:
    if workflow_key is not None and workflow_keys is not None:
        raise ValueError("runtime_reconciliation_invalid")
    configs = legacy_flow.load_all_configs()
    oa_api = legacy_api.build_api(configs[0])
    pending = production_pending.identify_pending_rows(
        oa_api.pending_rows(page_size=200, max_pages=100),
        configs,
    )
    selected = [
        item
        for item in pending
        if item["support_status"] == "supported"
        and (workflow_key is None or item["flow_key"] == workflow_key)
        and (workflow_keys is None or item["flow_key"] in workflow_keys)
    ]
    return reconciliation.reconcile(
        root,
        pending_rows=[
            {
                "request_id": item["requestId"],
                "received_at": item["received_at"],
                "workflow_key": item["flow_key"],
            }
            for item in selected
        ],
        handled_rows=oa_api.handled_rows(page_size=200, max_pages=100),
        workflow_key=workflow_key,
    )


def _persisted_mode(action_file: Path) -> str | None:
    try:
        payload = attempt_state.read_action(action_file)
    except (OSError, ValueError):
        return None
    return str(payload.get("mode") or "") if isinstance(payload, dict) else None


def _persisted_workflow(action_file: Path) -> str | None:
    try:
        payload = attempt_state.read_action(action_file)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    workflow_key = str(payload.get("workflow_key") or "")
    return workflow_key if workflow_key in flow_engine.WORKFLOW_KEYS else None


def _matches_confirmed_unknown_recovery(
    action_file: Path,
    confirmed_request_id: str | None,
) -> bool:
    if not confirmed_request_id:
        return False
    try:
        action = attempt_state.read_action(action_file)
        state = json.loads(
            action_file.with_name("state.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    if not isinstance(action, dict) or not isinstance(state, dict):
        return False
    return (
        str(state.get("request_id") or "") == confirmed_request_id
        and action.get("action") == "needs_admin"
        and state.get("action") == "needs_admin"
        and action.get("execution_state") == "needs_admin"
        and state.get("execution_state") == "needs_admin"
        and action.get("write_state") == "unknown"
        and state.get("write_state") == "unknown"
        and action.get("reason") == "writer_result_unknown"
        and state.get("reason") == "writer_result_unknown"
    )


def _failure_payload(error: Exception, *, command: str = "next-action") -> dict:
    if command in {"status", "prune", "clean"}:
        raw_code = str(error).strip()
        code = raw_code if raw_code in _RUNTIME_FAILURE_CODES else "runtime_failed"
        failure = {"code": code, "stage": command}
        if (
            command == "prune"
            and code == "retention_plan_invalid"
            and isinstance(error.__cause__, ValueError)
            and str(error.__cause__) == "daily_metric_conflict"
        ):
            failure["detail_code"] = "daily_metric_conflict"
        return {
            "schema_version": 1,
            "kind": "preaudit_runtime_failed",
            "failure": failure,
        }
    if command in {"next-action", "complete-review"}:
        raw_code = str(error).strip()
        return {
            "schema_version": 1,
            "kind": "preaudit_action_failed",
            "execution_state": "failed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "failure": {
                "code": (
                    raw_code
                    if raw_code in _FLOW_FAILURE_CODES
                    else "flow_action_failed"
                ),
                "stage": command.replace("-", "_"),
                "message": "本地预审动作失败",
            },
        }
    raise AssertionError("unsupported public command")


def _fail(args: argparse.Namespace, error: Exception) -> int:
    failure = _failure_payload(error, command=args.command)
    _print_payload(failure)
    if args.command in {"status", "prune", "clean"}:
        print(failure["failure"]["code"], file=sys.stderr)
    return 1


def _invocation_outcome(error: Exception) -> str:
    code = str(error).strip()
    if "unreadable" in code or "path" in code or "file" in code:
        return "file_transfer_error"
    if "invalid" in code or "conflict" in code or "contract" in code:
        return "json_contract_error"
    return "other"


def _emit_invocation(
    sink: InvocationSink | None,
    *,
    command: str,
    workflow_key: str | None,
    outcome: str,
) -> None:
    if sink is None:
        return
    workflow_keys = (
        [workflow_key]
        if workflow_key in flow_engine.WORKFLOW_KEYS
        else ["global"]
        if command in {"status", "prune", "clean"}
        else []
    )
    for current_workflow in workflow_keys:
        try:
            sink(
                {
                    "workflow_key": current_workflow,
                    "command": command,
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "outcome": outcome,
                    "human_reproduction": "not_attempted",
                }
            )
        except Exception:
            continue


def _invocation_workflow(
    args: argparse.Namespace,
    payload: object = None,
) -> str | None:
    if (
        isinstance(payload, dict)
        and payload.get("workflow_key") in flow_engine.WORKFLOW_KEYS
    ):
        return str(payload["workflow_key"])
    workflow_key = getattr(args, "workflow", None)
    if workflow_key in flow_engine.WORKFLOW_KEYS:
        return str(workflow_key)
    action_file = getattr(args, "action_file", None)
    if isinstance(action_file, Path) and len(action_file.parents) >= 4:
        inferred = action_file.parents[3].name
        if inferred in flow_engine.WORKFLOW_KEYS:
            return inferred
    return None


def _attach_live_queue_projection(payload: dict) -> None:
    live = payload.get("live_check")
    reconciled = payload.get("reconciliation")
    operations = payload.get("operations")
    if not isinstance(operations, dict) or not isinstance(live, dict):
        return
    oa = live.get("oa")
    if not isinstance(oa, dict):
        return
    supported = oa.get("supported")
    unsupported = oa.get("unsupported")
    trusted_live = (
        live.get("schema_version") == 1
        and live.get("kind") == "preaudit_live_status"
        and oa.get("execution_identity") == "verified"
        and isinstance(supported, int)
        and not isinstance(supported, bool)
        and supported >= 0
        and isinstance(unsupported, int)
        and not isinstance(unsupported, bool)
        and unsupported >= 0
    )
    quarantine = (
        reconciled.get("quarantine")
        if isinstance(reconciled, dict)
        else None
    )
    actionable = (
        quarantine.get("actionable_pending")
        if isinstance(quarantine, dict)
        else None
    )
    quarantined = (
        quarantine.get("pending_quarantined")
        if isinstance(quarantine, dict)
        else None
    )
    trusted_reconciliation = (
        isinstance(reconciled, dict)
        and reconciled.get("schema_version") == 1
        and reconciled.get("kind") == "preaudit_reconciliation"
        and reconciled.get("read_only") is True
        and isinstance(actionable, int)
        and not isinstance(actionable, bool)
        and actionable >= 0
        and isinstance(quarantined, int)
        and not isinstance(quarantined, bool)
        and quarantined >= 0
        and actionable + quarantined == supported
    )
    operations["live_queue"] = (
        {
            "evidence_status": "exact",
            "remaining": supported,
            "actionable": actionable,
            "quarantined": quarantined,
            "unsupported": unsupported,
        }
        if trusted_live and trusted_reconciliation
        else {
            "evidence_status": "unknown",
            "remaining": supported if trusted_live else "unknown",
            "actionable": "unknown",
            "quarantined": "unknown",
            "unsupported": unsupported if trusted_live else "unknown",
        }
    )
    attention = (
        reconciled.get("failure_attention")
        if isinstance(reconciled, dict)
        else None
    )
    attention_fields = {
        "scope",
        "status",
        "affected_cases",
        "repeated_failure_cases",
        "long_wait_failure_cases",
        "long_wait_threshold_seconds",
        "max_failure_attempts",
        "oldest_wait_seconds",
        "failure_counts",
    }
    counts = attention.get("failure_counts") if isinstance(attention, dict) else None
    numeric_fields = attention_fields - {"scope", "status", "failure_counts"}
    trusted_attention = (
        trusted_live
        and trusted_reconciliation
        and isinstance(attention, dict)
        and set(attention) == attention_fields
        and attention.get("scope") == "live_pending"
        and attention.get("status") in {"clear", "attention_required"}
        and all(
            isinstance(attention.get(field), int)
            and not isinstance(attention.get(field), bool)
            and attention[field] >= 0
            for field in numeric_fields
        )
        and isinstance(counts, dict)
        and all(
            code in reconciliation.QUARANTINED_REASONS | {"unknown_failure"}
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
            for code, count in counts.items()
        )
        and sum(counts.values()) == attention.get("affected_cases")
        and (attention.get("status") == "clear")
        == (attention.get("affected_cases") == 0)
    )
    if trusted_attention:
        operations["failure_attention"] = attention


def main(
    argv: Sequence[str] | None = None,
    *,
    flow_engine_factory: FlowEngineFactory | None = None,
    runtime_status: RuntimeOperation | None = None,
    runtime_prune: RuntimeOperation | None = None,
    live_status: LiveStatusOperation | None = None,
    reconcile_status: ReconciliationOperation | None = None,
    invocation_sink: InvocationSink | None = None,
    persist_invocations: bool = False,
) -> int:
    args = build_parser().parse_args(argv)
    sink = invocation_sink
    if sink is None and persist_invocations:
        cli_log = (
            _runtime_root(getattr(args, "root", None))
            / ".metrics"
            / "cli-invocations.json"
        )
        sink = lambda receipt: observability.record_cli_invocation(cli_log, receipt)
    try:
        if args.command == "status":
            operation = runtime_status or runtime_maintenance.status
            root = _runtime_root(args.root)
            payload = dict(operation(root))
            if args.check_live:
                if live_status is None:
                    from .runtime_doctor import status as live_status_operation
                else:
                    live_status_operation = live_status
                payload["live_check"] = live_status_operation()
            if args.reconcile:
                reconcile_operation = reconcile_status or _live_reconciliation
                payload["reconciliation"] = {
                    **reconcile_operation(root),
                    "usage": {
                        "gate_scope": "startup_or_terminal",
                        "snapshot_consistency": "non_atomic",
                        "running_monitor": "original_execution_progress",
                    },
                }
            _attach_live_queue_projection(payload)
            payload["controller_handoff"] = controller_handoff.status()
            from . import session_alert
            payload["notification"] = session_alert.status()
            if args.output_file is not None:
                payload = _status_file_payload(payload, args.output_file)
            elif args.summary:
                from .case_recovery import summary
                payload = summary(payload)
        elif args.command == "prune":
            operation = runtime_prune or runtime_maintenance.prune
            payload = operation(_runtime_root(args.root))
        elif args.command == "clean":
            try:
                with runtime_control.global_controller_lease():
                    if (
                        runtime_control.writer_active()
                        or runtime_control.active_request_writers() != 0
                    ):
                        raise RuntimeError("cache_clean_runtime_active")
                    try:
                        with runtime_control.request_writer_lease(
                            runtime_control.GLOBAL_WRITER_KEY,
                            runtime_control.writer_lock_file(),
                            blocking=False,
                        ):
                            payload = attachment_cache.clean_all_cache()
                    except BlockingIOError as error:
                        raise RuntimeError(
                            "cache_clean_runtime_active"
                        ) from error
            except (BlockingIOError, RuntimeError) as error:
                if str(error) == "controller_already_active":
                    raise RuntimeError("cache_clean_runtime_active") from error
                raise
        else:
            if args.command == "next-action":
                controlled = args.mode == "controlled-write"
                if controlled != (args.confirm_write == "YES"):
                    raise ValueError("run_mode_unauthorized")
                confirmed_unknown_recovery = (
                    args.recover_action_file is not None
                    and args.confirm_recovery == "YES"
                    and args.confirm_unknown_no_write == "YES"
                    and _matches_confirmed_unknown_recovery(
                        args.recover_action_file,
                        args.confirm_unknown_request_id,
                    )
                )
                if (
                    controlled
                    and args.workflow == "travel_expense"
                    and not confirmed_unknown_recovery
                ):
                    raise ValueError("production_rolling_controller_required")
                if confirmed_unknown_recovery and (
                    runtime_control.controller_active()
                    or runtime_control.writer_active()
                    or runtime_control.active_request_writers() != 0
                ):
                    raise ValueError("recovery_runtime_active")
                engine = (
                    flow_engine_factory()
                    if flow_engine_factory is not None
                    else _live_flow_engine(controlled=controlled)
                )
                mode = "controlled_write" if controlled else "preview"
                if args.recover_action_file is not None:
                    if args.confirm_recovery != "YES":
                        raise ValueError("recovery_confirmation_required")
                    if _persisted_workflow(args.recover_action_file) != args.workflow:
                        raise ValueError("recovery_not_safe")
                    def recover() -> dict:
                        return engine.recover_confirmed_no_write(
                            args.recover_action_file,
                            mode=mode,
                            confirmed=True,
                            confirmed_unknown_no_write=(
                                args.confirm_unknown_no_write == "YES"
                            ),
                            confirmed_request_id=args.confirm_unknown_request_id,
                        )

                    if confirmed_unknown_recovery:
                        try:
                            with runtime_control.global_controller_lease():
                                if (
                                    runtime_control.writer_active()
                                    or runtime_control.active_request_writers() != 0
                                ):
                                    raise ValueError("recovery_runtime_active")
                                payload = recover()
                        except RuntimeError as error:
                            if str(error) != "controller_already_active":
                                raise
                            raise ValueError("recovery_runtime_active") from error
                    else:
                        payload = recover()
                else:
                    payload = engine.next_action(args.workflow, mode=mode)
            else:
                persisted_mode = _persisted_mode(args.action_file)
                controlled = persisted_mode == "controlled_write"
                if controlled != (args.confirm_write == "YES"):
                    raise ValueError("run_mode_unauthorized")
                if controlled and _persisted_workflow(args.action_file) == "travel_expense":
                    raise ValueError("production_rolling_controller_required")
                engine = (
                    flow_engine_factory()
                    if flow_engine_factory is not None
                    else _live_flow_engine(controlled=controlled)
                )
                payload = engine.complete_review(
                    args.action_file,
                    args.decision_file,
                )
    except Exception as error:
        _emit_invocation(
            sink,
            command=args.command,
            workflow_key=_invocation_workflow(args),
            outcome=_invocation_outcome(error),
        )
        return _fail(args, error)
    _emit_invocation(
        sink,
        command=args.command,
        workflow_key=_invocation_workflow(args, payload),
        outcome="success",
    )
    _print_payload(payload)
    return 0


def entrypoint(argv: Sequence[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    try:
        return main(raw, persist_invocations=True)
    except SystemExit as error:
        code = int(error.code or 0)
        if code == 0 or not raw or raw[0] not in {
            "next-action",
            "complete-review",
            "status",
            "prune",
            "clean",
        }:
            return code
        command = raw[0]
        root_value: Path | None = None
        workflow_key: str | None = None
        for option, assign in (
            ("--root", "root"),
            ("--workflow", "workflow"),
            ("--action-file", "action"),
        ):
            if option not in raw:
                continue
            index = raw.index(option)
            if index + 1 >= len(raw):
                continue
            value = raw[index + 1]
            if assign == "root":
                root_value = Path(value)
            elif assign == "workflow" and value in flow_engine.WORKFLOW_KEYS:
                workflow_key = value
            elif assign == "action":
                action_path = Path(value)
                if len(action_path.parents) >= 4:
                    inferred = action_path.parents[3].name
                    if inferred in flow_engine.WORKFLOW_KEYS:
                        workflow_key = inferred
        cli_log = _runtime_root(root_value) / ".metrics" / "cli-invocations.json"
        _emit_invocation(
            lambda receipt: observability.record_cli_invocation(cli_log, receipt),
            command=command,
            workflow_key=workflow_key,
            outcome="argument_error",
        )
        return code


if __name__ == "__main__":
    sys.exit(entrypoint())
