from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time
from typing import Callable, Sequence

from apps.oa_approval import attachment_cache, runtime_control, writer_outcomes
from apps.oa_finance_preaudit import (
    cli,
    codex_review_worker,
    concurrency_control,
    controller_handoff,
    flow_engine,
    process_pool,
    production_pending,
    reconciliation,
)
from apps.oa_finance_preaudit import runtime_doctor


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RenewActions = Callable[[list[Path]], list[Path]]
ReviewRunner = Callable[[list[dict], int], process_pool.PoolResult[dict]]
PrepareRunner = Callable[[int], process_pool.PoolResult[dict]]
PrepareSelection = str | dict[str, object]
PrepareAction = Callable[[PrepareSelection], dict]
BatchRunner = Callable[..., dict]
LiveStatus = Callable[[], dict]
PipelineRunner = Callable[[int, int], process_pool.PoolResult[dict]]
ReviewWorker = Callable[[dict], dict]
ReviewCompleter = Callable[[Path, dict], dict]
ExecutorFactory = Callable[[int], object]
QUARANTINED_REASONS = {
    "material_collection_failed",
    *writer_outcomes.CONFIRMED_NO_WRITE_CODES,
}
ROLLING_WORKFLOW_KEYS = frozenset(
    {
        "travel_expense",
        "business_entertainment",
        "business_support_fee",
        "small_marketing_fee",
    }
)


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("production_probe_state_invalid") from error
    if not isinstance(value, dict):
        raise ValueError("production_probe_state_invalid")
    return value


def _prepared_action_matches_selection(
    action_path: Path,
    action: dict,
    state: dict,
    selected: dict,
    workflow_key: str,
    *,
    project_root: Path = PROJECT_ROOT,
) -> bool:
    request_id = str(selected.get("requestId") or "").strip()
    received_at = str(selected.get("received_at") or "").strip()
    attempt = state.get("attempt")
    if (
        not request_id
        or not received_at
        or not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or attempt < 1
    ):
        return False
    case_key = hashlib.sha256(
        f"{request_id}\x1f{received_at}".encode("utf-8")
    ).hexdigest()
    root = Path(project_root).expanduser().resolve()
    expected_action = (
        root
        / "runtime"
        / "finance_preaudit_flows"
        / workflow_key
        / "cases"
        / case_key
        / f"attempt-{attempt}"
        / "action.json"
    )
    expected_state = expected_action.with_name("state.json")
    shared = {
        "action": "review_required",
        "execution_state": "review_required",
        "business_state": "not_available",
        "write_state": "authorized",
        "workflow_key": workflow_key,
        "case_key": case_key,
        "attempt": attempt,
        "mode": "controlled_write",
        "write_authorized": True,
        "action_file": str(expected_action),
        "state_file": str(expected_state),
    }
    return (
        action_path == expected_action
        and state.get("request_id") == request_id
        and state.get("received_at") == received_at
        and all(action.get(key) == value for key, value in shared.items())
        and all(state.get(key) == value for key, value in shared.items())
    )


def _open_action_files(project_root: Path) -> list[Path]:
    flow_root = project_root / "runtime" / "finance_preaudit_flows"
    if not flow_root.exists():
        return []
    opened: dict[tuple[str, str], Path] = {}
    identities: list[dict] = []
    for state in reconciliation.latest_states(flow_root):
        workflow_key = str(state.get("workflow_key") or "")
        if workflow_key not in ROLLING_WORKFLOW_KEYS:
            continue
        action_file = (
            flow_root
            / workflow_key
            / "cases"
            / str(state["case_key"])
            / f"attempt-{state['attempt']}"
            / "action.json"
        )
        if (
            state.get("execution_state") == "review_required"
            and state.get("write_state") == "authorized"
            and state.get("mode") == "controlled_write"
            and state.get("write_authorized") is True
            and state.get("action_file") == str(action_file)
            and action_file.is_file()
            and not action_file.is_symlink()
        ):
            request_id = str(state.get("request_id") or "")
            received_at = str(state.get("received_at") or "")
            identity = (request_id, received_at)
            opened[identity] = action_file
            identities.append(
                {
                    "requestId": request_id,
                    "received_at": received_at,
                    "flow_key": workflow_key,
                    "support_status": "supported",
                    "can_submit": True,
                }
            )
    ordered = production_pending.actionable_fifo(identities)
    return [
        opened[(str(item["requestId"]), str(item["received_at"]))]
        for item in ordered
    ]


def _renew_actions(action_files: list[Path]) -> list[Path]:
    engine = cli._live_flow_engine(controlled=True)
    flow_root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    renewed: list[Path] = []
    for action_file in action_files:
        action = _read_object(action_file)
        case_key = str(action.get("case_key") or "")
        workflow_key = str(action.get("workflow_key") or "")
        if workflow_key not in flow_engine.WORKFLOW_KEYS:
            raise ValueError("production_probe_renew_failed")
        workflow_root = flow_root / workflow_key
        try:
            with flow_engine._exclusive_lock(workflow_root / "flow.lock"):
                current = engine._resume_active_action(
                    workflow_root,
                    case_key=case_key,
                )
        except Exception as error:
            runtime_control.request_stop("production_probe_renew_failed")
            raise ValueError("production_probe_renew_failed") from error
        if (
            current is None
            or current.get("action") != "review_required"
            or current.get("action_file") != str(action_file)
            or current.get("write_state") != "authorized"
        ):
            runtime_control.request_stop("production_probe_renew_failed")
            raise ValueError("production_probe_renew_failed")
        renewed.append(action_file)
    return renewed


def _historical_write_authorization_failures() -> frozenset[Path]:
    flow_root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    historical: set[Path] = set()
    for state in reconciliation.latest_states(flow_root):
        if (
            state.get("workflow_key") not in ROLLING_WORKFLOW_KEYS
            or state.get("execution_state") != "needs_admin"
            or state.get("reason") != "write_authorization_invalid"
            or state.get("write_state") != "not_requested"
        ):
            continue
        action_file = (
            flow_root
            / str(state["workflow_key"])
            / "cases"
            / str(state["case_key"])
            / f"attempt-{state['attempt']}"
            / "action.json"
        )
        if state.get("action_file") != str(action_file):
            raise ValueError("production_probe_state_invalid")
        historical.add(action_file)
    return frozenset(historical)


def _local_quarantined_identities() -> frozenset[tuple[str, str]]:
    flow_root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    identities: set[tuple[str, str]] = set()
    for state in reconciliation.latest_states(flow_root):
        reason = state.get("reason")
        if (
            state.get("execution_state") == "needs_admin"
            and state.get("write_state") == "not_requested"
            and reason in {"write_authorization_invalid", *QUARANTINED_REASONS}
        ):
            request_id = str(state.get("request_id") or "").strip()
            received_at = str(state.get("received_at") or "").strip()
            if not request_id or not received_at:
                raise ValueError("production_probe_state_invalid")
            identities.add((request_id, received_at))
    return frozenset(identities)


def _local_unknown_quarantined_identities() -> frozenset[tuple[str, str]]:
    flow_root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    identities: set[tuple[str, str]] = set()
    for state in reconciliation.latest_states(flow_root):
        if (
            state.get("execution_state") == "needs_admin"
            and state.get("write_state") == "unknown"
            and state.get("reason") in reconciliation.QUARANTINED_REASONS
        ):
            request_id = str(state.get("request_id") or "").strip()
            received_at = str(state.get("received_at") or "").strip()
            if not request_id or not received_at:
                raise ValueError("production_probe_state_invalid")
            identities.add((request_id, received_at))
    return frozenset(identities)


def _historical_unknown_action_files(
    identities: set[tuple[str, str]],
    *,
    workflow_key: str,
) -> frozenset[Path]:
    if not identities:
        return frozenset()
    flow_root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    action_files: set[Path] = set()
    for state in reconciliation.latest_states(flow_root):
        identity = (
            str(state.get("request_id") or "").strip(),
            str(state.get("received_at") or "").strip(),
        )
        if (
            identity not in identities
            or state.get("workflow_key") != workflow_key
            or state.get("execution_state") != "needs_admin"
            or state.get("write_state") != "unknown"
            or state.get("reason") not in reconciliation.QUARANTINED_REASONS
        ):
            continue
        action_files.add(
            flow_root
            / workflow_key
            / "cases"
            / str(state["case_key"])
            / f"attempt-{state['attempt']}"
            / "action.json"
        )
    return frozenset(action_files)


def _recovery_candidates(
    action_files: Sequence[Path],
    *,
    assigned_identities: set[tuple[str, str]],
) -> list[dict]:
    flow_root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    candidates: list[dict] = []
    seen_identities: set[tuple[str, str]] = set()
    for raw_path in action_files:
        action_path = Path(raw_path).expanduser()
        resolved = action_path.resolve()
        state_path = resolved.with_name("state.json")
        if (
            not action_path.is_absolute()
            or action_path.is_symlink()
            or action_path != resolved
            or not resolved.is_relative_to(flow_root)
            or not flow_engine._is_attempt_action_path(resolved, flow_root)
            or state_path.is_symlink()
        ):
            raise ValueError("production_probe_recovery_invalid")
        state = _read_object(state_path)
        request_id = str(state.get("request_id") or "").strip()
        received_at = str(state.get("received_at") or "").strip()
        workflow_key = str(state.get("workflow_key") or "").strip()
        identity = (request_id, received_at)
        if identity in assigned_identities:
            continue
        if (
            not request_id
            or not received_at
            or workflow_key not in ROLLING_WORKFLOW_KEYS
            or state.get("action_file") != str(resolved)
            or state.get("execution_state") != "needs_admin"
            or state.get("write_state") != "not_requested"
            or state.get("reason") not in QUARANTINED_REASONS
            or identity in seen_identities
        ):
            raise ValueError("production_probe_recovery_invalid")
        seen_identities.add(identity)
        candidates.append(
            {
                "requestId": request_id,
                "received_at": received_at,
                "flow_key": workflow_key,
                "support_status": "supported",
                "can_submit": True,
                "recovery_action_file": str(resolved),
            }
        )
    return candidates


def _prepare_one(selection: PrepareSelection) -> dict:
    assigned_identities: set[tuple[str, str]] = set()
    historical_unknown_identities: set[tuple[str, str]] = set()
    recovery_action_files: tuple[Path, ...] = ()
    if isinstance(selection, str):
        workflow_key = selection
    elif isinstance(selection, dict) and selection.get("workflow_key") == "*":
        workflow_key = "*"
        raw_assigned = selection.get("assigned_identities")
        raw_historical_unknown = selection.get(
            "historical_unknown_identities",
            [],
        )
        raw_recovery_actions = selection.get("recover_action_files", [])
        if not isinstance(raw_assigned, list) or not isinstance(
            raw_historical_unknown,
            list,
        ) or not isinstance(raw_recovery_actions, list) or any(
            not isinstance(path, str) or not path
            for path in raw_recovery_actions
        ):
            return {
                "action": "failed",
                "error_code": "production_probe_prepare_failed",
            }
        for identity in raw_assigned:
            if (
                not isinstance(identity, list)
                or len(identity) != 2
                or not all(isinstance(value, str) and value for value in identity)
            ):
                return {
                    "action": "failed",
                    "error_code": "production_probe_prepare_failed",
                }
            assigned_identities.add((identity[0], identity[1]))
        for identity in raw_historical_unknown:
            if (
                not isinstance(identity, list)
                or len(identity) != 2
                or not all(isinstance(value, str) and value for value in identity)
            ):
                return {
                    "action": "failed",
                    "error_code": "production_probe_prepare_failed",
                }
            historical_unknown_identities.add((identity[0], identity[1]))
        recovery_action_files = tuple(Path(path) for path in raw_recovery_actions)
    else:
        return {
            "action": "failed",
            "error_code": "production_probe_prepare_failed",
        }
    if runtime_control.stop_requested():
        return {"action": "failed", "error_code": "production_probe_stopped"}
    try:
        historical_authorization_failures = (
            _historical_write_authorization_failures()
        )
        quarantine_budget = _local_quarantine_count() + 1
    except ValueError:
        runtime_control.request_stop("production_probe_prepare_failed")
        return {
            "action": "failed",
            "error_code": "production_probe_prepare_failed",
        }
    quarantined = 0
    while quarantined < quarantine_budget:
        if runtime_control.stop_requested():
            return {
                "action": "failed",
                "error_code": "production_probe_stopped",
            }
        selected: dict | None = None
        recovery_action_file: Path | None = None
        selected_workflow = workflow_key
        if workflow_key == "*":
            try:
                live_candidates = production_pending.actionable_fifo(
                    cli._live_pending_identities(),
                    excluded_identities=(
                        _local_quarantined_identities()
                        | historical_unknown_identities
                        | assigned_identities
                    ),
                    allowed_workflows=ROLLING_WORKFLOW_KEYS,
                )
            except (ValueError, OSError):
                runtime_control.request_stop("production_probe_prepare_failed")
                return {
                    "action": "failed",
                    "error_code": "production_probe_prepare_failed",
                }
            try:
                recovery_candidates = production_pending.actionable_fifo(
                    _recovery_candidates(
                        recovery_action_files,
                        assigned_identities=assigned_identities,
                    ),
                    allowed_workflows=ROLLING_WORKFLOW_KEYS,
                )
            except (ValueError, OSError):
                runtime_control.request_stop("production_probe_prepare_failed")
                return {
                    "action": "failed",
                    "error_code": "production_probe_recovery_failed",
                }
            candidates = production_pending.actionable_fifo(
                [*live_candidates, *recovery_candidates],
                allowed_workflows=ROLLING_WORKFLOW_KEYS,
            )
            if not candidates:
                runtime_control.request_stop("production_probe_prepare_failed")
                return {
                    "action": "failed",
                    "error_code": "production_probe_prepare_failed",
                }
            selected = candidates[0]
            selected_workflow = str(selected.get("flow_key") or "")
            if selected.get("recovery_action_file") is not None:
                recovery_action_file = Path(selected["recovery_action_file"])
        if selected is None:
            engine = cli._live_flow_engine(controlled=True)
        elif recovery_action_file is not None:
            engine = cli._live_flow_engine(controlled=True)
        else:
            engine = cli._live_flow_engine(
                controlled=True,
                selected_identity=(
                    str(selected["requestId"]),
                    str(selected["received_at"]),
                ),
            )
        selected_workflow_root = (
            PROJECT_ROOT
            / "runtime"
            / "finance_preaudit_flows"
            / selected_workflow
        )
        workflow_authorization_failures = frozenset(
            action_file
            for action_file in historical_authorization_failures
            if action_file.is_relative_to(selected_workflow_root)
        )
        while True:
            try:
                if recovery_action_file is not None:
                    action = engine.recover_confirmed_no_write(
                        recovery_action_file,
                        mode="controlled_write",
                        confirmed=True,
                    )
                else:
                    action = engine.next_action(
                        selected_workflow,
                        mode="controlled_write",
                        skip_write_authorization_failures=(
                            workflow_authorization_failures
                        ),
                        skip_unknown_failures=_historical_unknown_action_files(
                            historical_unknown_identities,
                            workflow_key=selected_workflow,
                        ),
                    )
            except ValueError as error:
                if str(error) == "completion_in_progress":
                    if runtime_control.stop_requested():
                        return {
                            "action": "failed",
                            "error_code": "production_probe_stopped",
                        }
                    time.sleep(0.05)
                    continue
                runtime_control.request_stop("production_probe_prepare_failed")
                return {
                    "action": "failed",
                    "error_code": (
                        "production_probe_recovery_failed"
                        if recovery_action_file is not None
                        else "production_probe_prepare_failed"
                    ),
                }
            except Exception:
                runtime_control.request_stop("production_probe_prepare_failed")
                return {
                    "action": "failed",
                    "error_code": (
                        "production_probe_recovery_failed"
                        if recovery_action_file is not None
                        else "production_probe_prepare_failed"
                    ),
                }
            break
        if runtime_control.stop_requested():
            return {
                "action": "failed",
                "error_code": "production_probe_stopped",
            }
        if recovery_action_file is not None and action.get("action") != "review_required":
            runtime_control.request_stop("production_probe_prepare_failed")
            return {
                "action": "failed",
                "error_code": "production_probe_recovery_failed",
            }
        if action.get("action") == "review_required":
            if selected is not None:
                persisted_action: dict = {}
                persisted: dict = {}
                raw_action_path = Path(str(action.get("action_file") or ""))
                action_path = raw_action_path.resolve()
                state_path = action_path.with_name("state.json")
                try:
                    if (
                        raw_action_path.is_absolute()
                        and not raw_action_path.is_symlink()
                        and raw_action_path == action_path
                        and not state_path.is_symlink()
                    ):
                        persisted_action = _read_object(action_path)
                        persisted = _read_object(state_path)
                except (KeyError, ValueError):
                    pass
                if (
                    not _prepared_action_matches_selection(
                        action_path,
                        persisted_action,
                        persisted,
                        selected,
                        selected_workflow,
                    )
                ):
                    runtime_control.request_stop(
                        "production_probe_prepare_failed"
                    )
                    return {
                        "action": "failed",
                        "error_code": "production_probe_fifo_mismatch",
                    }
            return {
                "action": "review_required",
                "action_file": action["action_file"],
            }
        if (
            action.get("action") == "needs_admin"
            and (
                action.get("reason") in QUARANTINED_REASONS
            )
        ):
            quarantined += 1
            try:
                current_quarantine_count = _local_quarantine_count()
            except ValueError:
                runtime_control.request_stop("production_probe_prepare_failed")
                return {
                    "action": "failed",
                    "error_code": "production_probe_prepare_failed",
                }
            quarantine_budget = max(
                quarantine_budget,
                current_quarantine_count + 1,
            )
            continue
        error_code = str(
            action.get("reason") or "production_probe_prepare_failed"
        )
        try:
            runtime_control.request_stop(error_code)
        except ValueError:
            runtime_control.request_stop()
        return {
            "action": str(action.get("action") or "failed"),
            "error_code": error_code,
        }
    return {
        "action": "failed",
        "error_code": "production_probe_quarantine_exhausted",
    }


def _prepare_actions(count: int) -> process_pool.PoolResult[dict]:
    return process_pool.run(
        ["travel_expense"] * count,
        _prepare_one,
        max_workers=count,
        batch_timeout_sec=600,
        task_timeout_sec=300,
        heartbeat_interval_sec=10,
        runtime_status=concurrency_control.sample_local_health,
    )


def _run_reviews(
    tasks: list[dict],
    capacity: int,
) -> process_pool.PoolResult[dict]:
    return process_pool.run(
        tasks,
        codex_review_worker.review,
        max_workers=capacity,
        batch_timeout_sec=1200,
        task_timeout_sec=960,
        heartbeat_interval_sec=15,
        runtime_status=concurrency_control.sample_local_health,
    )


def _reviewable_action_files(project_root: Path) -> list[Path]:
    flow_root = (
        project_root
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
    )
    if not flow_root.parent.exists():
        return []
    selected: list[Path] = []
    for state in reconciliation.latest_states(flow_root.parent):
        if (
            state.get("workflow_key") != "travel_expense"
            or state.get("execution_state") != "closed"
            or state.get("write_state") != "submitted"
            or state.get("business_state") not in {"pass", "needs_human"}
        ):
            continue
        action_file = (
            flow_root
            / "cases"
            / str(state["case_key"])
            / f"attempt-{state['attempt']}"
            / "action.json"
        )
        try:
            action = _read_object(action_file)
            review_packet_file = Path(
                str(action["review_packet_file"])
            ).resolve(strict=True)
            review_packet = _read_object(review_packet_file)
            material_file = Path(
                str(review_packet["material_file"])
            ).resolve(strict=True)
        except (KeyError, OSError, ValueError):
            continue
        attempt_root = action_file.parent.resolve()
        if (
            action.get("workflow_key") == "travel_expense"
            and action.get("action") == "closed"
            and action.get("execution_state") == "closed"
            and action.get("write_state") == "submitted"
            and action.get("action_file") == str(action_file)
            and codex_review_worker._read_completed(action_file)
            == state.get("business_state")
            and review_packet.get("workflow_key") == "travel_expense"
            and review_packet_file.is_relative_to(attempt_root)
            and material_file.is_relative_to(attempt_root)
            and not action_file.is_symlink()
            and not review_packet_file.is_symlink()
            and not material_file.is_symlink()
        ):
            selected.append(action_file.resolve())
    return sorted(selected)


def _run_read_only_reviews(
    tasks: list[dict],
    capacity: int,
) -> process_pool.PoolResult[dict]:
    return process_pool.run(
        tasks,
        codex_review_worker.run_read_only,
        max_workers=capacity,
        batch_timeout_sec=1800,
        task_timeout_sec=960,
        heartbeat_interval_sec=15,
        runtime_status=concurrency_control.sample_local_health,
    )


def _aggregate_codex_events(values: list[dict]) -> dict:
    counts: Counter[str] = Counter()
    attempts = 0
    attempts_known = True
    recorded_attempts = 0
    unknown_attempts = 0
    recorded_workers = 0
    for value in values:
        events = value.get("codex_events") if isinstance(value, dict) else None
        current_process_attempts = (
            value.get("process_attempts") if isinstance(value, dict) else None
        )
        if (
            not isinstance(current_process_attempts, int)
            or isinstance(current_process_attempts, bool)
            or current_process_attempts < 0
        ):
            attempts_known = False
            continue
        attempts += current_process_attempts
        if not codex_review_worker._valid_events(
            events,
            attempts=current_process_attempts,
        ):
            unknown_attempts += current_process_attempts
            continue
        current_recorded = events.get("recorded_attempts")
        current_unknown = events.get("unknown_attempts")
        current_counts = events.get("counts")
        if (
            isinstance(current_recorded, int)
            and not isinstance(current_recorded, bool)
            and current_recorded >= 0
        ):
            recorded_attempts += current_recorded
        if (
            isinstance(current_unknown, int)
            and not isinstance(current_unknown, bool)
            and current_unknown >= 0
        ):
            unknown_attempts += current_unknown
        if events.get("status") == "recorded":
            recorded_workers += 1
        if isinstance(current_counts, dict):
            for event_type, count in current_counts.items():
                if (
                    isinstance(event_type, str)
                    and isinstance(count, int)
                    and not isinstance(count, bool)
                    and count >= 0
                ):
                    counts[event_type] += count
    workers = len(values)
    status = (
        "recorded"
        if workers > 0 and recorded_workers == workers
        else "unknown"
    )
    return {
        "status": status,
        "workers": workers,
        "recorded_workers": recorded_workers,
        "unknown_workers": workers - recorded_workers,
        "attempts": attempts if attempts_known else None,
        "recorded_attempts": recorded_attempts,
        "unknown_attempts": unknown_attempts if attempts_known else None,
        "counts": dict(sorted(counts.items())),
    }


def _merge_codex_event_summaries(summaries: list[dict]) -> dict:
    counts: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    count_fields = (
        "workers",
        "recorded_workers",
        "unknown_workers",
        "recorded_attempts",
    )
    attempts_known = True
    for summary in summaries:
        for field in count_fields:
            value = summary.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[field] += value
        for field in ("attempts", "unknown_attempts"):
            value = summary.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[field] += value
            else:
                attempts_known = False
        current_counts = summary.get("counts")
        if isinstance(current_counts, dict):
            for event_type, count in current_counts.items():
                if isinstance(event_type, str) and isinstance(count, int):
                    counts[event_type] += count
    return {
        "status": (
            "recorded"
            if totals["workers"] > 0
            and totals["recorded_workers"] == totals["workers"]
            else "unknown"
        ),
        **{field: totals[field] for field in count_fields},
        "attempts": totals["attempts"] if attempts_known else None,
        "unknown_attempts": (
            totals["unknown_attempts"] if attempts_known else None
        ),
        "counts": dict(sorted(counts.items())),
    }


def run_read_only_probe(
    *,
    capacity: int,
    sample_size: int = 20,
    project_root: Path = PROJECT_ROOT,
    run_reviews: ReviewRunner = _run_read_only_reviews,
) -> dict:
    if (
        not isinstance(capacity, int)
        or isinstance(capacity, bool)
        or not 1 <= capacity <= 12
    ):
        raise ValueError("read_only_probe_capacity_invalid")
    if (
        not isinstance(sample_size, int)
        or isinstance(sample_size, bool)
        or not capacity <= sample_size <= 100
    ):
        raise ValueError("read_only_probe_sample_size_invalid")
    root = Path(project_root).expanduser().resolve()
    selected = _reviewable_action_files(root)[:sample_size]
    if len(selected) != sample_size:
        raise ValueError("read_only_probe_insufficient_samples")
    tasks = [
        {
            "project_root": str(root),
            "action_file": str(action_file),
            "timeout_seconds": 900,
        }
        for action_file in selected
    ]
    result = run_reviews(tasks, capacity)
    business_results: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    process_states: Counter[str] = Counter()
    decision_states: Counter[str] = Counter()
    exit_codes: Counter[str] = Counter()
    usage_statuses: Counter[str] = Counter()
    token_totals: Counter[str] = Counter()
    completed = 0
    failed = result.failed
    for outcome in result.outcomes:
        value = outcome.value if isinstance(outcome.value, dict) else {}
        if outcome.status == "completed" and value.get("status") == "completed":
            completed += 1
            business_state = value.get("business_state")
            if isinstance(business_state, str):
                business_results[business_state] += 1
        elif outcome.status == "completed":
            failed += 1
        error_code = value.get("error_code") or outcome.error_code
        if isinstance(error_code, str):
            error_counts[error_code] += 1
        process_states[str(value.get("process_state") or "unknown")] += 1
        decision_states[str(value.get("decision_state") or "unknown")] += 1
        exit_code = value.get("exit_code")
        exit_codes[str(exit_code) if isinstance(exit_code, int) else "none"] += 1
        usage = value.get("token_usage")
        usage_status = usage.get("status") if isinstance(usage, dict) else None
        if usage_status not in {"recorded", "partial"}:
            usage_status = "unknown"
        usage_statuses[usage_status] += 1
        if isinstance(usage, dict):
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "total_tokens",
            ):
                count = usage.get(field)
                if isinstance(count, int) and not isinstance(count, bool):
                    token_totals[field] += count
    codex_events = _aggregate_codex_events(
        [
            outcome.value if isinstance(outcome.value, dict) else {}
            for outcome in result.outcomes
        ]
    )
    transient_error_events = codex_events["counts"].get("error")
    if transient_error_events is None and codex_events["status"] == "recorded":
        transient_error_events = 0
    return {
        "schema_version": 1,
        "kind": "preaudit_read_only_concurrency_probe",
        "capacity": capacity,
        "sample_size": sample_size,
        "completed": completed,
        "failed": failed,
        "timed_out": result.timed_out,
        "stopped": result.stopped,
        "peak_active": result.peak_active,
        "elapsed_seconds": result.elapsed_seconds,
        "business_results": dict(sorted(business_results.items())),
        "error_counts": dict(sorted(error_counts.items())),
        "process_states": dict(sorted(process_states.items())),
        "decision_states": dict(sorted(decision_states.items())),
        "codex_events": codex_events,
        "transient_error_events": transient_error_events,
        "exit_codes": dict(sorted(exit_codes.items())),
        "model": codex_review_worker.MODEL,
        "reasoning_effort": codex_review_worker.REASONING_EFFORT,
        "production_write": False,
        "production_stop_preserved": runtime_control.stop_requested(),
        "runtime_health": concurrency_control.summarize_runtime(
            result.runtime_samples
        ),
        "watchdog": {
            "runtime_samples": len(result.runtime_samples),
            "maximum_task_seconds": result.max_task_elapsed_seconds,
            "task_timeout_seconds": 960,
        },
        "token_usage": {
            "status": (
                "recorded"
                if usage_statuses["recorded"] == len(result.outcomes)
                else "partial"
                if usage_statuses["recorded"] or usage_statuses["partial"]
                else "unknown"
            ),
            "recorded_workers": usage_statuses["recorded"],
            "partial_workers": usage_statuses["partial"],
            "unknown_workers": usage_statuses["unknown"],
            **{
                field: token_totals[field]
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "total_tokens",
                )
            },
        },
    }


def run_adaptive_read_only_probe(
    *,
    start_capacity: int = 6,
    sample_size: int = 20,
    max_waves: int = 6,
    probe_runner: Callable[..., dict] = run_read_only_probe,
) -> dict:
    if (
        start_capacity not in concurrency_control.CAPACITY_STEPS
        or not isinstance(max_waves, int)
        or isinstance(max_waves, bool)
        or not 1 <= max_waves <= 20
    ):
        raise ValueError("adaptive_probe_settings_invalid")
    capacity = start_capacity
    healthy_windows = 0
    waves: list[dict] = []
    decisions: list[dict] = []
    token_totals: Counter[str] = Counter()
    status = "completed"
    for _index in range(max_waves):
        wave = probe_runner(capacity=capacity, sample_size=sample_size)
        waves.append(wave)
        usage = wave.get("token_usage")
        if isinstance(usage, dict):
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "total_tokens",
            ):
                count = usage.get(field)
                if isinstance(count, int) and not isinstance(count, bool):
                    token_totals[field] += count
        decision = concurrency_control.decide_capacity(
            current_capacity=capacity,
            healthy_windows=healthy_windows,
            wave={
                "requested": int(wave.get("sample_size") or 0),
                "completed": int(wave.get("completed") or 0),
                "failed": int(wave.get("failed") or 0),
                "timed_out": int(wave.get("timed_out") or 0),
                "stopped": int(wave.get("stopped") or 0),
                "transient_error_events": wave.get("transient_error_events"),
                "codex_event_status": (
                    wave.get("codex_events", {}).get("status")
                    if isinstance(wave.get("codex_events"), dict)
                    else "unknown"
                ),
            },
            host=(
                wave.get("runtime_health")
                if isinstance(wave.get("runtime_health"), dict)
                else {"status": "unknown"}
            ),
        )
        decisions.append(decision)
        capacity = int(decision["next_capacity"])
        healthy_windows = int(decision["healthy_windows"])
        if decision["action"] == "stop":
            status = "stopped"
            break
    codex_events = _merge_codex_event_summaries(
        [
            wave["codex_events"]
            for wave in waves
            if isinstance(wave.get("codex_events"), dict)
        ]
    )
    transient_error_events = codex_events["counts"].get("error")
    if transient_error_events is None and codex_events["status"] == "recorded":
        transient_error_events = 0
    return {
        "schema_version": 1,
        "kind": "preaudit_adaptive_read_only_probe",
        "status": status,
        "start_capacity": start_capacity,
        "recommended_capacity": capacity,
        "waves": len(waves),
        "last_decision": decisions[-1],
        "elapsed_seconds": sum(
            float(wave.get("elapsed_seconds") or 0) for wave in waves
        ),
        "completed": sum(int(wave.get("completed") or 0) for wave in waves),
        "failed": sum(int(wave.get("failed") or 0) for wave in waves),
        "token_usage": dict(token_totals),
        "codex_events": codex_events,
        "transient_error_events": transient_error_events,
        "production_write": False,
    }


def _review_only(task: dict) -> dict:
    return codex_review_worker.review(task)


def _process_executor(capacity: int) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(
        max_workers=capacity,
        mp_context=multiprocessing.get_context("spawn"),
    )


def _preparation_executor() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=os.setsid,
    )


def _merge_review_attempts(attempts: list[dict]) -> dict:
    current = dict(attempts[-1])
    process_attempts = sum(int(item.get("process_attempts") or 0) for item in attempts)
    usages = [
        item.get("token_usage")
        for item in attempts
        if isinstance(item.get("token_usage"), dict)
    ]
    recorded_usages = [
        usage for usage in usages if usage.get("status") in {"recorded", "partial"}
    ]
    token_usage = {
        "status": (
            "recorded"
            if len(usages) == len(attempts)
            and usages
            and all(usage.get("status") == "recorded" for usage in usages)
            else "partial"
            if recorded_usages
            else "unknown"
        ),
        **{
            field: sum(int(usage.get(field) or 0) for usage in recorded_usages)
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "total_tokens",
            )
        },
    }
    event_values = [
        item.get("codex_events")
        for item in attempts
        if isinstance(item.get("codex_events"), dict)
    ]
    event_counts: Counter[str] = Counter()
    recorded_events = 0
    for events in event_values:
        recorded_events += int(events.get("recorded_attempts") or 0)
        counts = events.get("counts")
        if isinstance(counts, dict):
            event_counts.update(
                {
                    str(key): int(value)
                    for key, value in counts.items()
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                }
            )
    codex_events = {
        "status": (
            "recorded"
            if process_attempts > 0 and recorded_events == process_attempts
            else "unknown"
        ),
        "attempts": process_attempts,
        "recorded_attempts": recorded_events,
        "unknown_attempts": max(0, process_attempts - recorded_events),
        "counts": dict(sorted(event_counts.items())),
    }
    return {
        **current,
        "process_attempts": process_attempts,
        "process_retries": max(0, process_attempts - 1),
        "token_usage": token_usage,
        "codex_events": codex_events,
        "agent_wall_seconds": sum(
            int(item.get("agent_wall_seconds") or 0) for item in attempts
        ),
    }


def _terminate_executor(
    executor: object,
    *,
    owned_process_groups: bool = False,
) -> bool:
    processes = list((getattr(executor, "_processes", None) or {}).values())
    owned_groups: set[int] = set()
    if owned_process_groups:
        for process in processes:
            if not process.is_alive() or process.pid is None:
                continue
            try:
                if os.getpgid(process.pid) == process.pid:
                    owned_groups.add(process.pid)
            except OSError:
                continue
    for process in processes:
        if process.pid in owned_groups:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
        elif process.is_alive():
            process.terminate()
    for process in processes:
        process.join(timeout=1)
    for group in owned_groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except OSError:
            pass
    for process in processes:
        if process.is_alive() and process.pid not in owned_groups:
            process.kill()
        process.join(timeout=1)
    executor.shutdown(wait=False, cancel_futures=True)
    return all(not process.is_alive() for process in processes)


def _run_dispatched_tasks(
    count: int,
    capacity: int,
    *,
    project_root: Path = PROJECT_ROOT,
    renew_actions: RenewActions = _renew_actions,
    prepare_action: PrepareAction | None = None,
    review_worker: ReviewWorker = _review_only,
    complete_review: ReviewCompleter = codex_review_worker.complete_review_attempt,
    executor_factory: ExecutorFactory = _process_executor,
    task_timeout_sec: float = 960,
    prepare_timeout_sec: float = 300,
    heartbeat_interval_sec: float = 15,
    runtime_status: Callable[[], dict] = concurrency_control.sample_local_health,
    strict_global_fifo: bool = False,
    adaptive: bool = False,
    max_capacity: int | None = None,
    historical_unknown_identities: frozenset[tuple[str, str]] = frozenset(),
    recovery_action_files: Sequence[Path] = (),
) -> process_pool.PoolResult[dict]:
    maximum_capacity = capacity if max_capacity is None else max_capacity
    recovery_queue = tuple(
        Path(path).expanduser().resolve() for path in recovery_action_files
    )
    if (
        not 1 <= capacity <= process_pool.MAX_PROCESS_POOL_CAPACITY
        or not capacity <= maximum_capacity <= process_pool.MAX_PROCESS_POOL_CAPACITY
        or task_timeout_sec <= 0
        or prepare_timeout_sec <= 0
        or heartbeat_interval_sec <= 0
        or (recovery_queue and not strict_global_fifo)
        or len(recovery_queue) > count
        or len(set(recovery_queue)) != len(recovery_queue)
    ):
        raise ValueError("production_dispatcher_watchdog_invalid")
    if adaptive and (
        capacity not in concurrency_control.CAPACITY_STEPS
        or maximum_capacity not in concurrency_control.CAPACITY_STEPS
    ):
        raise ValueError("production_dispatcher_watchdog_invalid")
    started_at = time.monotonic()
    root = Path(project_root).expanduser().resolve()
    outcomes: list[process_pool.TaskOutcome[dict] | None] = [None] * count
    assigned: set[Path] = set()
    assigned_identities: dict[Path, tuple[str, str]] = {}
    futures: dict[object, tuple[int, float, float, Path]] = {}
    write_ready: dict[int, tuple[float, Path, dict, float, int]] = {}
    writer: (
        tuple[Future[dict], int, float, Path, dict, float, float, int] | None
    ) = None
    review_histories: dict[int, list[dict]] = {}
    decision_retries: set[int] = set()
    next_index = 0
    next_write_index = 0
    current_review_capacity = capacity
    adaptive_healthy_windows = 0
    adaptive_window_target = capacity * 2
    adaptive_window_values: list[dict] = []
    adaptive_window_hosts: list[dict] = []
    capacity_decisions: list[dict] = []
    adaptive_pressure_active = False
    started = 0
    peak_active = 0
    halted = False
    force_terminated = False
    runtime_samples: list[dict] = []
    max_task_elapsed_seconds = 0.0
    next_heartbeat = time.monotonic()
    preparation: tuple[Future[dict], int, float] | None = None
    preparation_stop_deadline: float | None = None
    preparation_deferred = False
    unblocked_duplicate_retries = 0
    write_queue_capacity = max(2, maximum_capacity * 2)
    max_write_queue_depth = 0
    prepare_action = prepare_action or _prepare_one
    prepare_executor = _preparation_executor()
    prepare_executor_terminated = False
    executor = executor_factory(maximum_capacity)
    reaping_succeeded = True
    writer_executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="oa-global-writer",
    )

    def terminate_executor(
        target: object,
        *,
        owned_process_groups: bool = False,
    ) -> None:
        nonlocal reaping_succeeded
        reaped = _terminate_executor(
            target,
            owned_process_groups=owned_process_groups,
        )
        reaping_succeeded = reaping_succeeded and reaped

    def stopped_review(
        index: int,
        prepare_elapsed: float,
        value: dict | None,
    ) -> None:
        outcomes[index] = process_pool.TaskOutcome(
            index=index,
            status="stopped",
            value=(
                {
                    **value,
                    "status": "stopped",
                    "error_code": "stop_requested",
                    "prepare_elapsed_seconds": prepare_elapsed,
                }
                if isinstance(value, dict)
                else None
            ),
            error_code="stop_requested",
        )

    def harvest_stopped_reviews(completed_futures: set[object]) -> None:
        assignments = sorted(
            (
                futures[future][0],
                future,
            )
            for future in completed_futures
            if future in futures
        )
        for _index, future in assignments:
            index, prepare_elapsed, _task_started, _action_file = futures.pop(
                future
            )
            try:
                value = future.result()
            except Exception:
                outcomes[index] = process_pool.TaskOutcome(
                    index=index,
                    status="failed",
                    value=None,
                    error_code="worker_process_failed",
                )
                continue
            if value.get("status") == "reviewed":
                stopped_review(index, prepare_elapsed, value)
            else:
                outcomes[index] = process_pool.TaskOutcome(
                    index=index,
                    status="completed",
                    value={
                        **value,
                        "prepare_elapsed_seconds": prepare_elapsed,
                    },
                )

    def stop_reviews() -> None:
        nonlocal force_terminated
        if force_terminated:
            return
        harvest_stopped_reviews(
            {future for future in futures if future.done()}
        )
        for _future, (
            index,
            prepare_elapsed,
            _task_started,
            _action_file,
        ) in futures.items():
            stopped_review(index, prepare_elapsed, None)
        futures.clear()
        terminate_executor(executor)
        force_terminated = True

    def stop_write_queue() -> None:
        for index in sorted(write_ready):
            prepare_elapsed, _action, reviewed, _ready_at, _depth = (
                write_ready[index]
            )
            stopped_review(index, prepare_elapsed, reviewed)
        write_ready.clear()

    def request_pipeline_stop() -> None:
        nonlocal halted
        halted = True
        runtime_control.request_stop()
        stop_reviews()
        stop_write_queue()

    def observe_adaptive_window(value: dict) -> None:
        nonlocal adaptive_healthy_windows
        nonlocal adaptive_window_target
        nonlocal current_review_capacity
        nonlocal adaptive_pressure_active
        if not adaptive:
            return
        adaptive_window_values.append(value)
        if len(adaptive_window_values) < adaptive_window_target:
            return
        codex_events = _aggregate_codex_events(adaptive_window_values)
        transient_errors = codex_events["counts"].get("error")
        if transient_errors is None and codex_events["status"] == "recorded":
            transient_errors = 0
        try:
            host = runtime_status()
        except Exception:
            host = {"status": "unknown"}
        adaptive_window_hosts.append(host)
        window_host = concurrency_control.summarize_runtime(
            tuple({"host": item} for item in adaptive_window_hosts)
        )
        if (
            adaptive_pressure_active
            and window_host.get("status") == "pressure"
        ):
            decision = {
                "action": "hold",
                "next_capacity": current_review_capacity,
                "healthy_windows": 0,
                "reason": "host_pressure_active",
            }
        else:
            decision = concurrency_control.decide_capacity(
                current_capacity=current_review_capacity,
                healthy_windows=adaptive_healthy_windows,
                wave={
                    "requested": len(adaptive_window_values),
                    "completed": len(adaptive_window_values),
                    "failed": 0,
                    "timed_out": 0,
                    "stopped": 0,
                    "codex_event_status": codex_events["status"],
                    "transient_error_events": transient_errors,
                },
                host=window_host,
            )
        if int(decision["next_capacity"]) > maximum_capacity:
            decision = {
                "action": "hold",
                "next_capacity": maximum_capacity,
                "healthy_windows": 1,
                "reason": "configured_maximum",
            }
        capacity_decisions.append(decision)
        if decision["reason"] == "host_pressure":
            adaptive_pressure_active = True
        elif window_host.get("status") == "healthy":
            adaptive_pressure_active = False
        adaptive_healthy_windows = int(decision["healthy_windows"])
        current_review_capacity = min(
            maximum_capacity,
            int(decision["next_capacity"]),
        )
        adaptive_window_values.clear()
        adaptive_window_hosts.clear()
        adaptive_window_target = current_review_capacity * 2
        if decision["action"] == "stop":
            request_pipeline_stop()

    def apply_immediate_host_pressure(host: dict) -> None:
        nonlocal adaptive_healthy_windows
        nonlocal adaptive_pressure_active
        nonlocal adaptive_window_target
        nonlocal current_review_capacity
        if not adaptive or adaptive_pressure_active:
            return
        decision = concurrency_control.decide_capacity(
            current_capacity=current_review_capacity,
            healthy_windows=adaptive_healthy_windows,
            wave={
                "requested": 0,
                "completed": 0,
                "failed": 0,
                "timed_out": 0,
                "stopped": 0,
            },
            host=host,
        )
        capacity_decisions.append(decision)
        current_review_capacity = int(decision["next_capacity"])
        adaptive_healthy_windows = 0
        adaptive_pressure_active = True
        adaptive_window_values.clear()
        adaptive_window_hosts.clear()
        adaptive_window_target = current_review_capacity * 2

    def submit_review(
        index: int,
        prepare_elapsed: float,
        action_file: Path,
        *,
        first_attempt: bool,
    ) -> None:
        nonlocal started, peak_active
        future = executor.submit(
            review_worker,
            {
                "project_root": str(root),
                "action_file": str(action_file),
                "timeout_seconds": 900,
            },
        )
        futures[future] = (
            index,
            prepare_elapsed,
            time.monotonic(),
            action_file,
        )
        if first_attempt:
            started += 1
        peak_active = max(peak_active, len(futures))

    def validated_assigned_identity(action_file: Path) -> tuple[str, str]:
        action_path = action_file.resolve()
        state_path = action_path.with_name("state.json")
        if (
            not action_file.is_absolute()
            or action_file.is_symlink()
            or action_file != action_path
            or state_path.is_symlink()
        ):
            raise ValueError("production_probe_state_invalid")
        action = _read_object(action_path)
        state = _read_object(state_path)
        request_id = str(state.get("request_id") or "").strip()
        received_at = str(state.get("received_at") or "").strip()
        workflow_key = str(state.get("workflow_key") or "").strip()
        selected = {
            "requestId": request_id,
            "received_at": received_at,
        }
        if (
            workflow_key not in ROLLING_WORKFLOW_KEYS
            or not _prepared_action_matches_selection(
                action_path,
                action,
                state,
                selected,
                workflow_key,
                project_root=root,
            )
        ):
            raise ValueError("production_probe_state_invalid")
        return request_id, received_at

    def global_prepare_selection() -> dict:
        identities: list[list[str]] = []
        for action_file in sorted(assigned):
            identity = assigned_identities.get(action_file)
            if identity is None:
                raise ValueError("production_probe_state_invalid")
            identities.append(list(identity))
        selection: dict[str, object] = {
            "workflow_key": "*",
            "assigned_identities": identities,
            "historical_unknown_identities": [
                list(identity)
                for identity in sorted(historical_unknown_identities)
            ],
        }
        if recovery_queue:
            selection["recover_action_files"] = [
                str(path) for path in recovery_queue
            ]
        return selection

    try:
        while (
            futures
            or preparation is not None
            or writer is not None
            or write_ready
            or (next_index < count and not halted)
        ):
            if runtime_control.stop_requested() and not halted:
                halted = True
                stop_reviews()
                stop_write_queue()

            now = time.monotonic()
            if writer is not None and writer[0].done():
                (
                    writer_future,
                    index,
                    prepare_elapsed,
                    action_file,
                    submitted_review,
                    review_ready_at,
                    writer_started_at,
                    enqueue_depth,
                ) = writer
                writer = None
                preparation_deferred = False
                try:
                    completed = writer_future.result()
                except Exception:
                    completed = {
                        "status": "failed",
                        "business_state": "not_available",
                        "write_state": "unknown",
                        "error_code": "writer_result_unknown",
                    }
                value = {
                    **completed,
                    "prepare_elapsed_seconds": prepare_elapsed,
                    "review_done_to_write_start_seconds": round(
                        max(0.0, writer_started_at - review_ready_at),
                        6,
                    ),
                    "writer_seconds": round(
                        max(0.0, time.monotonic() - writer_started_at),
                        6,
                    ),
                    "write_queue_depth_at_enqueue": enqueue_depth,
                }
                if (
                    value.get("status") == "retry_required"
                    and value.get("error_code") == "codex_decision_invalid"
                    and index not in decision_retries
                    and not halted
                ):
                    decision_retries.add(index)
                    review_histories[index] = [submitted_review]
                    submit_review(
                        index,
                        prepare_elapsed,
                        action_file,
                        first_attempt=False,
                    )
                else:
                    if value.get("status") == "retry_required":
                        value = {**value, "status": "failed"}
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="completed",
                        value=value,
                    )
                    review_histories.pop(index, None)
                    if value.get("status") != "completed":
                        request_pipeline_stop()
                    else:
                        next_write_index += 1
                        observe_adaptive_window(value)

            review_ages = {
                index: max(0.0, now - task_started)
                for (
                    index,
                    _prepare_elapsed,
                    task_started,
                    _action_file,
                ) in futures.values()
            }
            active_ages = dict(review_ages)
            prepare_age = 0.0
            if preparation is not None:
                _prepare_future, prepare_index, prepare_started_at = preparation
                prepare_age = max(0.0, now - prepare_started_at)
                active_ages[prepare_index] = prepare_age
            max_task_elapsed_seconds = max(
                max_task_elapsed_seconds,
                max(active_ages.values(), default=0.0),
            )
            timed_out_indices = {
                index
                for future, (
                    index,
                    _prepare_elapsed,
                    _task_started,
                    _action_file,
                ) in futures.items()
                if not future.done()
                and review_ages[index] >= task_timeout_sec
            }
            prepare_timed_out = (
                preparation is not None
                and not preparation[0].done()
                and prepare_age >= prepare_timeout_sec
            )
            if timed_out_indices or prepare_timed_out:
                for _future, (
                    index,
                    _prepare_elapsed,
                    _started_at,
                    _action_file,
                ) in futures.items():
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="timed_out",
                        value=None,
                        error_code=(
                            "worker_task_timeout"
                            if index in timed_out_indices
                            else "worker_cancelled_after_timeout"
                        ),
                    )
                futures.clear()
                if preparation is not None:
                    _prepare_future, prepare_index, _started_at = preparation
                    outcomes[prepare_index] = process_pool.TaskOutcome(
                        index=prepare_index,
                        status="timed_out",
                        value=None,
                        error_code=(
                            "production_probe_prepare_timeout"
                            if prepare_timed_out
                            else "worker_cancelled_after_timeout"
                        ),
                    )
                    terminate_executor(
                        prepare_executor,
                        owned_process_groups=True,
                    )
                    prepare_executor_terminated = True
                    preparation = None
                halted = True
                runtime_control.request_stop()
                stop_write_queue()
                if not force_terminated:
                    terminate_executor(executor)
                    force_terminated = True
                continue

            if now >= next_heartbeat:
                try:
                    host = runtime_status()
                except Exception:
                    host = {"status": "unknown"}
                runtime_samples.append(
                    {
                        "elapsed_seconds": round(now - started_at, 3),
                        "pending_tasks": max(0, count - next_index),
                        "active_workers": len(futures)
                        + int(preparation is not None),
                        "active_reviews": len(futures),
                        "review_capacity": current_review_capacity,
                        "writer_active": writer is not None,
                        "write_queue_depth": len(write_ready),
                        "write_queue_capacity": write_queue_capacity,
                        "max_write_queue_depth": max_write_queue_depth,
                        "completed_tasks": sum(
                            outcome is not None for outcome in outcomes
                        ),
                        "oldest_task_seconds": round(
                            max(active_ages.values(), default=0.0),
                            3,
                        ),
                        "host": host,
                    }
                )
                if adaptive and host.get("status") == "pressure":
                    apply_immediate_host_pressure(host)
                elif adaptive:
                    if host.get("status") == "healthy":
                        adaptive_pressure_active = False
                    adaptive_window_hosts.append(host)
                next_heartbeat = now + heartbeat_interval_sec
                if host.get("status") == "critical" and not halted:
                    request_pipeline_stop()

            if (
                halted
                and preparation is not None
                and not preparation[0].done()
            ):
                if preparation_stop_deadline is None:
                    preparation_stop_deadline = time.monotonic() + min(
                        5.0,
                        prepare_timeout_sec,
                    )
                if time.monotonic() >= preparation_stop_deadline:
                    _future, prepare_index, _started_at = preparation
                    outcomes[prepare_index] = process_pool.TaskOutcome(
                        index=prepare_index,
                        status="stopped",
                        value=None,
                        error_code="stop_requested",
                    )
                    terminate_executor(
                        prepare_executor,
                        owned_process_groups=True,
                    )
                    prepare_executor_terminated = True
                    preparation = None
                    preparation_stop_deadline = None

            if preparation is not None and preparation[0].done():
                prepare_future, index, prepare_started_at = preparation
                prepare_elapsed = time.monotonic() - prepare_started_at
                try:
                    prepared = prepare_future.result()
                except Exception:
                    prepared = {
                        "action": "failed",
                        "error_code": "production_probe_prepare_failed",
                    }
                preparation = None
                preparation_stop_deadline = None
                if prepared.get("action") != "review_required":
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="completed",
                        value={
                            "status": "failed",
                            "business_state": "not_available",
                            "error_code": str(
                                prepared.get("error_code")
                                or "production_probe_prepare_failed"
                            ),
                            "prepare_elapsed_seconds": prepare_elapsed,
                            "process_attempts": 0,
                            "process_retries": 0,
                            "token_usage": {"status": "unknown"},
                        },
                    )
                    request_pipeline_stop()
                    if not prepare_executor_terminated:
                        terminate_executor(
                            prepare_executor,
                            owned_process_groups=True,
                        )
                        prepare_executor_terminated = True
                else:
                    action_file = Path(str(prepared["action_file"])).resolve()
                    if action_file in assigned:
                        next_index -= 1
                        if futures or write_ready or writer is not None:
                            preparation_deferred = True
                        else:
                            unblocked_duplicate_retries += 1
                            if unblocked_duplicate_retries > 1:
                                outcomes[index] = process_pool.TaskOutcome(
                                    index=index,
                                    status="completed",
                                    value={
                                        "status": "failed",
                                        "business_state": "not_available",
                                        "error_code": (
                                            "production_probe_duplicate_action"
                                        ),
                                        "prepare_elapsed_seconds": prepare_elapsed,
                                        "process_attempts": 0,
                                        "process_retries": 0,
                                        "token_usage": {"status": "unknown"},
                                    },
                                )
                                request_pipeline_stop()
                    elif halted or runtime_control.stop_requested():
                        stopped_review(index, prepare_elapsed, None)
                        halted = True
                    else:
                        unblocked_duplicate_retries = 0
                        if strict_global_fifo:
                            try:
                                assigned_identities[action_file] = (
                                    validated_assigned_identity(action_file)
                                )
                            except ValueError:
                                outcomes[index] = process_pool.TaskOutcome(
                                    index=index,
                                    status="completed",
                                    value={
                                        "status": "failed",
                                        "business_state": "not_available",
                                        "error_code": (
                                            "production_probe_prepare_failed"
                                        ),
                                        "prepare_elapsed_seconds": (
                                            prepare_elapsed
                                        ),
                                        "process_attempts": 0,
                                        "process_retries": 0,
                                        "token_usage": {"status": "unknown"},
                                    },
                                )
                                request_pipeline_stop()
                                continue
                        assigned.add(action_file)
                        submit_review(
                            index,
                            prepare_elapsed,
                            action_file,
                            first_attempt=True,
                        )

            done = {future for future in futures if future.done()}
            reviewed_now: list[tuple[int, float, Path, dict]] = []
            failed_now = False
            for future in sorted(done, key=lambda item: futures[item][0]):
                index, prepare_elapsed, _started_at, action_file = futures.pop(
                    future
                )
                try:
                    value = future.result()
                except Exception:
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="failed",
                        value=None,
                        error_code="worker_process_failed",
                    )
                    failed_now = True
                    continue
                if value.get("status") != "reviewed":
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="completed",
                        value={
                            **value,
                            "prepare_elapsed_seconds": prepare_elapsed,
                        },
                    )
                    failed_now = True
                    continue
                reviewed_now.append(
                    (index, prepare_elapsed, action_file, value)
                )
            if failed_now:
                for index, prepare_elapsed, _action, reviewed in reviewed_now:
                    stopped_review(index, prepare_elapsed, reviewed)
                request_pipeline_stop()
            elif halted:
                for index, prepare_elapsed, _action, reviewed in reviewed_now:
                    stopped_review(index, prepare_elapsed, reviewed)
            else:
                for index, prepare_elapsed, action_file, reviewed in reviewed_now:
                    merged = _merge_review_attempts(
                        [*review_histories.get(index, []), reviewed]
                    )
                    write_ready[index] = (
                        prepare_elapsed,
                        action_file,
                        merged,
                        time.monotonic(),
                        len(write_ready) + 1,
                    )
                    max_write_queue_depth = max(
                        max_write_queue_depth,
                        len(write_ready),
                    )

            if (
                not halted
                and writer is None
                and next_write_index in write_ready
                and not runtime_control.stop_requested()
            ):
                (
                    prepare_elapsed,
                    action_file,
                    reviewed,
                    review_ready_at,
                    enqueue_depth,
                ) = write_ready.pop(next_write_index)
                index = next_write_index
                writer_started_at = time.monotonic()
                writer = (
                    writer_executor.submit(
                        complete_review,
                        action_file,
                        reviewed,
                    ),
                    index,
                    prepare_elapsed,
                    action_file,
                    reviewed,
                    review_ready_at,
                    writer_started_at,
                    enqueue_depth,
                )

            while (
                not halted
                and preparation is None
                and next_index < count
                and len(futures) < current_review_capacity
                and (
                    len(futures)
                    + len(write_ready)
                    + int(preparation is not None)
                    < write_queue_capacity
                )
                and not preparation_deferred
            ):
                prepare_started = time.monotonic()
                available = (
                    []
                    if strict_global_fifo
                    else [
                        path
                        for path in _open_action_files(root)
                        if path not in assigned
                    ]
                )
                if not available:
                    try:
                        selection = (
                            global_prepare_selection()
                            if strict_global_fifo
                            else "*"
                        )
                    except ValueError:
                        outcomes[next_index] = process_pool.TaskOutcome(
                            index=next_index,
                            status="completed",
                            value={
                                "status": "failed",
                                "business_state": "not_available",
                                "error_code": "production_probe_prepare_failed",
                                "prepare_elapsed_seconds": 0.0,
                                "process_attempts": 0,
                                "process_retries": 0,
                                "token_usage": {"status": "unknown"},
                            },
                        )
                        next_index += 1
                        request_pipeline_stop()
                        break
                    preparation = (
                        prepare_executor.submit(
                            prepare_action,
                            selection,
                        ),
                        next_index,
                        time.monotonic(),
                    )
                    preparation_stop_deadline = None
                    next_index += 1
                    break
                renewed = renew_actions([available[0]])
                prepare_elapsed = time.monotonic() - prepare_started
                if renewed != [available[0]]:
                    outcomes[next_index] = process_pool.TaskOutcome(
                        index=next_index,
                        status="completed",
                        value={
                            "status": "failed",
                            "business_state": "not_available",
                            "error_code": "production_probe_renew_failed",
                            "prepare_elapsed_seconds": prepare_elapsed,
                            "process_attempts": 0,
                            "process_retries": 0,
                            "token_usage": {"status": "unknown"},
                        },
                    )
                    next_index += 1
                    request_pipeline_stop()
                    break
                action_file = available[0]
                assigned.add(action_file)
                submit_review(
                    next_index,
                    prepare_elapsed,
                    action_file,
                    first_attempt=True,
                )
                next_index += 1

            if (
                not futures
                and preparation is None
                and writer is None
                and not write_ready
                and (halted or next_index >= count)
            ):
                break
            waitables = [*futures]
            if preparation is not None:
                waitables.append(preparation[0])
            if writer is not None:
                waitables.append(writer[0])
            if waitables:
                wait(
                    waitables,
                    timeout=min(
                        0.25,
                        heartbeat_interval_sec,
                        task_timeout_sec / 4,
                    ),
                    return_when=FIRST_COMPLETED,
                )
            else:
                time.sleep(min(0.01, heartbeat_interval_sec))
    finally:
        known_processes = [
            *(getattr(prepare_executor, "_processes", None) or {}).values(),
            *(getattr(executor, "_processes", None) or {}).values(),
        ]
        if not prepare_executor_terminated:
            if preparation is not None:
                terminate_executor(
                    prepare_executor,
                    owned_process_groups=True,
                )
            else:
                prepare_executor.shutdown(wait=True)
        if not force_terminated:
            executor.shutdown(wait=True)
        writer_executor.shutdown(wait=True)
    processes_reaped = reaping_succeeded and all(
        not process.is_alive() for process in known_processes
    )

    for index, outcome in enumerate(outcomes):
        if outcome is None:
            outcomes[index] = process_pool.TaskOutcome(
                index=index,
                status="stopped",
                value=None,
                error_code="stop_requested",
            )
    completed_outcomes = tuple(
        outcome for outcome in outcomes if outcome is not None
    )
    if halted and not force_terminated and not runtime_control.stop_requested():
        runtime_control.request_stop()
    return process_pool.PoolResult(
        requested=count,
        started=started,
        completed=sum(
            outcome.status == "completed" for outcome in completed_outcomes
        ),
        failed=sum(outcome.status == "failed" for outcome in completed_outcomes),
        timed_out=sum(
            outcome.status == "timed_out" for outcome in completed_outcomes
        ),
        stopped=sum(
            outcome.status == "stopped" for outcome in completed_outcomes
        ),
        peak_active=peak_active,
        elapsed_seconds=time.monotonic() - started_at,
        outcomes=completed_outcomes,
        runtime_samples=tuple(runtime_samples),
        max_task_elapsed_seconds=round(max_task_elapsed_seconds, 3),
        processes_reaped=processes_reaped,
        recommended_capacity=current_review_capacity,
        capacity_decisions=tuple(capacity_decisions),
    )


def _run_global_dispatched_tasks(
    count: int,
    capacity: int,
    *,
    adaptive: bool = False,
    max_capacity: int | None = None,
    historical_unknown_identities: frozenset[tuple[str, str]] | None = None,
    recovery_action_files: Sequence[Path] = (),
) -> process_pool.PoolResult[dict]:
    codex_review_worker._reviewer_environment()
    frozen_unknown = (
        _local_unknown_quarantined_identities()
        if historical_unknown_identities is None
        else historical_unknown_identities
    )
    dispatcher_args = {
        "strict_global_fifo": True,
        "adaptive": adaptive,
        "max_capacity": max_capacity,
        "historical_unknown_identities": frozen_unknown,
    }
    if recovery_action_files:
        dispatcher_args["recovery_action_files"] = recovery_action_files
    return _run_dispatched_tasks(count, capacity, **dispatcher_args)


def _legacy_run_existing_batch(
    *,
    capacity: int,
    project_root: Path = PROJECT_ROOT,
    prepare_actions: PrepareRunner = _prepare_actions,
    renew_actions: RenewActions = _renew_actions,
    run_reviews: ReviewRunner = _run_reviews,
) -> dict:
    if not isinstance(capacity, int) or isinstance(capacity, bool) or not 1 <= capacity <= 8:
        raise ValueError("production_probe_capacity_invalid")
    root = Path(project_root).expanduser().resolve()
    if runtime_control.stop_requested():
        raise ValueError("production_probe_stopped")
    selected = _open_action_files(root)[:capacity]
    prepared_new = 0
    prepare_peak_active = 0
    prepare_elapsed_seconds = 0.0
    missing = capacity - len(selected)
    if missing:
        preparation = prepare_actions(missing)
        prepared_new = preparation.completed
        prepare_peak_active = preparation.peak_active
        prepare_elapsed_seconds = preparation.elapsed_seconds
        if (
            preparation.completed != missing
            or preparation.failed
            or preparation.timed_out
            or preparation.stopped
            or any(
                not isinstance(outcome.value, dict)
                or outcome.value.get("action") != "review_required"
                for outcome in preparation.outcomes
            )
        ):
            runtime_control.request_stop()
            raise ValueError("production_probe_prepare_failed")
        selected = _open_action_files(root)[:capacity]
    if len(selected) != capacity:
        raise ValueError("production_probe_insufficient_open_actions")
    renewed = renew_actions(selected)
    if renewed != selected:
        raise ValueError("production_probe_renew_failed")
    tasks = [
        {
            "project_root": str(root),
            "action_file": str(action_file),
            "timeout_seconds": 900,
        }
        for action_file in renewed
    ]
    result = run_reviews(tasks, capacity)
    business_results: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    completed = 0
    failed = 0
    process_attempts = 0
    process_retries = 0
    usage_statuses: Counter[str] = Counter()
    token_totals: Counter[str] = Counter()
    for outcome in result.outcomes:
        value = outcome.value if isinstance(outcome.value, dict) else {}
        business_state = value.get("business_state")
        worker_status = value.get("status")
        if outcome.status == "completed" and worker_status == "completed":
            completed += 1
        elif outcome.status not in {"timed_out", "stopped"}:
            failed += 1
        if worker_status == "completed" and isinstance(business_state, str):
            business_results[business_state] += 1
        error_code = value.get("error_code") or outcome.error_code
        if isinstance(error_code, str):
            error_counts[error_code] += 1
        process_attempts += int(value.get("process_attempts") or 0)
        process_retries += int(value.get("process_retries") or 0)
        token_usage = value.get("token_usage")
        if not isinstance(token_usage, dict):
            usage_statuses["unknown"] += 1
            continue
        usage_status = str(token_usage.get("status") or "unknown")
        if usage_status not in {"recorded", "partial"}:
            usage_status = "unknown"
        usage_statuses[usage_status] += 1
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            value_count = token_usage.get(field)
            if isinstance(value_count, int) and not isinstance(value_count, bool):
                token_totals[field] += value_count
    if failed or result.timed_out or result.stopped:
        runtime_control.request_stop()
    return {
        "schema_version": 1,
        "kind": "preaudit_production_concurrency_probe",
        "capacity": capacity,
        "selected": len(selected),
        "prepared_new": prepared_new,
        "prepare_peak_active": prepare_peak_active,
        "prepare_elapsed_seconds": prepare_elapsed_seconds,
        "completed": completed,
        "failed": failed,
        "timed_out": result.timed_out,
        "stopped": result.stopped,
        "peak_active": result.peak_active,
        "elapsed_seconds": result.elapsed_seconds,
        "business_results": dict(sorted(business_results.items())),
        "error_counts": dict(sorted(error_counts.items())),
        "model": codex_review_worker.MODEL,
        "reasoning_effort": codex_review_worker.REASONING_EFFORT,
        "process_attempts": process_attempts,
        "process_retries": process_retries,
        "runtime_health": concurrency_control.summarize_runtime(
            result.runtime_samples
        ),
        "watchdog": {
            "runtime_samples": len(result.runtime_samples),
            "maximum_task_seconds": result.max_task_elapsed_seconds,
            "task_timeout_seconds": 960,
        },
        "token_usage": {
            "status": (
                "recorded"
                if usage_statuses["recorded"] == len(result.outcomes)
                else "partial"
                if usage_statuses["recorded"] or usage_statuses["partial"]
                else "unknown"
            ),
            "recorded_workers": usage_statuses["recorded"],
            "partial_workers": usage_statuses["partial"],
            "unknown_workers": usage_statuses["unknown"],
            **{
                field: token_totals[field]
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "total_tokens",
                )
            },
        },
    }


def _travel_pending(live: dict) -> int:
    credentials = live.get("credentials")
    oa = live.get("oa")
    if (
        not isinstance(credentials, dict)
        or credentials.get("status") != "ok"
        or not isinstance(oa, dict)
        or oa.get("execution_identity") not in {"verified", "unknown"}
    ):
        raise ValueError("production_drain_live_status_invalid")
    by_workflow = oa.get("by_workflow")
    if not isinstance(by_workflow, dict):
        raise ValueError("production_drain_live_status_invalid")
    pending = by_workflow.get("travel_expense", 0)
    if not isinstance(pending, int) or isinstance(pending, bool) or pending < 0:
        raise ValueError("production_drain_live_status_invalid")
    if pending and oa.get("execution_identity") != "verified":
        raise ValueError("production_drain_live_status_invalid")
    return pending


def _production_reconciliation() -> dict:
    root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    return cli._live_reconciliation(
        root,
        workflow_keys=ROLLING_WORKFLOW_KEYS,
    )


def _supported_pending(live: dict) -> int:
    credentials = live.get("credentials")
    oa = live.get("oa")
    if (
        not isinstance(credentials, dict)
        or credentials.get("status") != "ok"
        or not isinstance(oa, dict)
        or oa.get("execution_identity") not in {"verified", "unknown"}
    ):
        raise ValueError("production_drain_live_status_invalid")
    by_workflow = oa.get("by_workflow")
    if not isinstance(by_workflow, dict):
        raise ValueError("production_drain_live_status_invalid")
    pending = 0
    for workflow_key in ROLLING_WORKFLOW_KEYS:
        count = by_workflow.get(workflow_key, 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("production_drain_live_status_invalid")
        pending += count
    if pending and oa.get("execution_identity") != "verified":
        raise ValueError("production_drain_live_status_invalid")
    return pending


def _rolling_snapshot(
    live_status: LiveStatus,
    reconcile_status: Callable[[], dict],
) -> tuple[int, int, int]:
    pending = _supported_pending(live_status())
    try:
        reconciled = reconcile_status()
        quarantine = reconciled["quarantine"]
        quarantined = quarantine["pending_quarantined"]
        actionable = quarantine["actionable_pending"]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("production_rolling_reconciliation_invalid") from error
    if (
        reconciled.get("schema_version") != 1
        or reconciled.get("kind") != "preaudit_reconciliation"
        or reconciled.get("read_only") is not True
        or not isinstance(quarantined, int)
        or isinstance(quarantined, bool)
        or not isinstance(actionable, int)
        or isinstance(actionable, bool)
        or quarantined < 0
        or actionable < 0
        or quarantined + actionable != pending
    ):
        raise ValueError("production_rolling_reconciliation_invalid")
    return pending, quarantined, actionable


def _quarantined_pending_count() -> int:
    return int(_production_reconciliation()["quarantine"]["pending_quarantined"])


def _local_quarantine_count() -> int:
    flow_root = (
        PROJECT_ROOT
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
    )
    return reconciliation.quarantine_summary(
        flow_root.parent,
        pending_rows=[],
    )["local_quarantined"]


def _legacy_run_drain(
    *,
    max_capacity: int,
    live_status: LiveStatus = runtime_doctor.status,
    batch_runner: BatchRunner = _legacy_run_existing_batch,
    on_batch: Callable[[dict], None] | None = None,
    max_batches: int = 100,
) -> dict:
    if not 1 <= max_capacity <= 6:
        raise ValueError("production_drain_capacity_invalid")
    started_at = time.monotonic()
    initial_pending = _travel_pending(live_status())
    pending = initial_pending
    batches: list[dict] = []
    while pending:
        if len(batches) >= max_batches:
            runtime_control.request_stop()
            raise ValueError("production_drain_batch_limit_reached")
        batch = batch_runner(capacity=min(max_capacity, pending))
        batches.append(batch)
        if on_batch is not None:
            on_batch(batch)
        if (
            batch.get("failed")
            or batch.get("timed_out")
            or batch.get("stopped")
            or batch.get("completed") != batch.get("selected")
        ):
            runtime_control.request_stop()
            raise ValueError("production_drain_batch_failed")
        pending = _travel_pending(live_status())

    business_results: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    token_totals: Counter[str] = Counter()
    usage_workers: Counter[str] = Counter()
    for batch in batches:
        business_results.update(batch.get("business_results") or {})
        error_counts.update(batch.get("error_counts") or {})
        usage = batch.get("token_usage") or {}
        for field in ("recorded_workers", "partial_workers", "unknown_workers"):
            usage_workers[field] += int(usage.get(field) or 0)
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            token_totals[field] += int(usage.get(field) or 0)
    return {
        "schema_version": 1,
        "kind": "preaudit_production_drain",
        "status": "completed",
        "model": codex_review_worker.MODEL,
        "reasoning_effort": codex_review_worker.REASONING_EFFORT,
        "max_capacity": max_capacity,
        "initial_pending": initial_pending,
        "final_pending": pending,
        "batches": len(batches),
        "selected": sum(int(batch.get("selected") or 0) for batch in batches),
        "prepared_new": sum(int(batch.get("prepared_new") or 0) for batch in batches),
        "completed": sum(int(batch.get("completed") or 0) for batch in batches),
        "process_attempts": sum(int(batch.get("process_attempts") or 0) for batch in batches),
        "process_retries": sum(int(batch.get("process_retries") or 0) for batch in batches),
        "batch_elapsed_seconds": sum(float(batch.get("elapsed_seconds") or 0) for batch in batches),
        "elapsed_seconds": time.monotonic() - started_at,
        "business_results": dict(sorted(business_results.items())),
        "error_counts": dict(sorted(error_counts.items())),
        "token_usage": {
            "status": (
                "recorded"
                if usage_workers["recorded_workers"] == sum(
                    int(batch.get("selected") or 0) for batch in batches
                )
                else "partial"
                if usage_workers["recorded_workers"] or usage_workers["partial_workers"]
                else "unknown"
            ),
            **usage_workers,
            **token_totals,
        },
    }


def run_existing_batch(*, capacity: int, **_legacy_options: object) -> dict:
    del capacity, _legacy_options
    raise ValueError("production_legacy_scheduler_removed")


def run_drain(*, max_capacity: int, **_legacy_options: object) -> dict:
    del max_capacity, _legacy_options
    raise ValueError("production_legacy_scheduler_removed")


def _pipeline_percentiles(values: list[float]) -> dict:
    if not values:
        return {"samples": 0, "p50": "unknown", "p95": "unknown"}
    ordered = sorted(max(0.0, float(value)) for value in values)

    def nearest(percent: int) -> float:
        rank = max(1, (len(ordered) * percent + 99) // 100)
        return round(ordered[rank - 1], 6)

    return {
        "samples": len(ordered),
        "p50": nearest(50),
        "p95": nearest(95),
    }


def _rolling_wave_metrics(result: process_pool.PoolResult[dict]) -> dict:
    business_results: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    usage_workers: Counter[str] = Counter()
    token_totals: Counter[str] = Counter()
    completed = 0
    failed = 0
    process_attempts = 0
    process_retries = 0
    prepare_active_seconds = 0.0
    worker_values: list[dict] = []
    review_to_write: list[float] = []
    writer_seconds: list[float] = []
    queue_depths: list[int] = []
    for outcome in result.outcomes:
        value = outcome.value if isinstance(outcome.value, dict) else {}
        worker_values.append(value)
        worker_status = value.get("status")
        if outcome.status == "completed" and worker_status == "completed":
            completed += 1
            business_state = value.get("business_state")
            if isinstance(business_state, str):
                business_results[business_state] += 1
        elif outcome.status not in {"timed_out", "stopped"}:
            failed += 1
        error_code = value.get("error_code") or outcome.error_code
        if isinstance(error_code, str):
            error_counts[error_code] += 1
        process_attempts += int(value.get("process_attempts") or 0)
        process_retries += int(value.get("process_retries") or 0)
        prepare_active_seconds += float(value.get("prepare_elapsed_seconds") or 0)
        review_wait = value.get("review_done_to_write_start_seconds")
        if isinstance(review_wait, (int, float)) and not isinstance(
            review_wait,
            bool,
        ):
            review_to_write.append(float(review_wait))
        writer_elapsed = value.get("writer_seconds")
        if isinstance(writer_elapsed, (int, float)) and not isinstance(
            writer_elapsed,
            bool,
        ):
            writer_seconds.append(float(writer_elapsed))
        queue_depth = value.get("write_queue_depth_at_enqueue")
        if isinstance(queue_depth, int) and not isinstance(queue_depth, bool):
            queue_depths.append(queue_depth)
        usage = value.get("token_usage")
        status = usage.get("status") if isinstance(usage, dict) else "unknown"
        if status not in {"recorded", "partial"}:
            status = "unknown"
        usage_workers[f"{status}_workers"] += 1
        if isinstance(usage, dict):
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "total_tokens",
            ):
                count = usage.get(field)
                if isinstance(count, int) and not isinstance(count, bool):
                    token_totals[field] += count
    codex_events = _aggregate_codex_events(worker_values)
    transient_error_events = codex_events["counts"].get("error")
    if transient_error_events is None and codex_events["status"] == "recorded":
        transient_error_events = 0
    review_samples = [
        sample
        for sample in result.runtime_samples
        if isinstance(sample.get("active_reviews"), int)
        and isinstance(sample.get("write_queue_capacity"), int)
        and sample["write_queue_capacity"] >= 2
    ]
    active_review_sum = sum(
        int(sample["active_reviews"]) for sample in review_samples
    )
    review_capacity_sum = sum(
        max(
            1,
            int(
                sample.get("review_capacity")
                or int(sample["write_queue_capacity"]) // 2
            ),
        )
        for sample in review_samples
    )
    sampled_queue_depths = [
        int(sample.get("max_write_queue_depth") or 0)
        for sample in result.runtime_samples
    ]
    queue_capacities = [
        int(sample["write_queue_capacity"])
        for sample in review_samples
    ]
    writer_busy_seconds = sum(writer_seconds)
    pipeline = {
        "review_done_to_write_start_seconds": _pipeline_percentiles(
            review_to_write
        ),
        "writer": {
            "max_concurrency": 1 if writer_seconds else 0,
            "busy_seconds": round(writer_busy_seconds, 6),
            "busy_ratio": round(
                min(
                    1.0,
                    writer_busy_seconds / max(result.elapsed_seconds, 1e-9),
                ),
                6,
            ),
        },
        "review_slots": {
            "samples": len(review_samples),
            "utilization": (
                round(active_review_sum / review_capacity_sum, 6)
                if review_capacity_sum
                else "unknown"
            ),
        },
        "write_queue": {
            "capacity": max(queue_capacities, default=0),
            "max_depth": max(
                [*queue_depths, *sampled_queue_depths],
                default=0,
            ),
        },
    }
    return {
        "requested": result.requested,
        "started": result.started,
        "completed": completed,
        "failed": failed + result.failed,
        "timed_out": result.timed_out,
        "stopped": result.stopped,
        "peak_active": result.peak_active,
        "elapsed_seconds": result.elapsed_seconds,
        "prepare_active_seconds": prepare_active_seconds,
        "process_attempts": process_attempts,
        "process_retries": process_retries,
        "runtime_health": concurrency_control.summarize_runtime(
            result.runtime_samples
        ),
        "watchdog": {
            "runtime_samples": len(result.runtime_samples),
            "maximum_task_seconds": result.max_task_elapsed_seconds,
            "task_timeout_seconds": 960,
        },
        "business_results": dict(sorted(business_results.items())),
        "error_counts": dict(sorted(error_counts.items())),
        "codex_events": codex_events,
        "transient_error_events": transient_error_events,
        "pipeline": pipeline,
        "processes_reaped": result.processes_reaped,
        "recommended_capacity": result.recommended_capacity,
        "capacity_decisions": list(result.capacity_decisions),
        "_pipeline_review_wait_samples": review_to_write,
        "_pipeline_active_review_sum": active_review_sum,
        "_pipeline_review_capacity_sum": review_capacity_sum,
        "token_usage": {
            "status": (
                "recorded"
                if usage_workers["recorded_workers"] == len(result.outcomes)
                else "partial"
                if usage_workers["recorded_workers"] or usage_workers["partial_workers"]
                else "unknown"
            ),
            **usage_workers,
            **token_totals,
        },
    }


def run_rolling_drain(
    *,
    capacity: int,
    adaptive: bool = False,
    max_capacity: int = 12,
    live_status: LiveStatus = runtime_doctor.status,
    pipeline_runner: PipelineRunner = _run_global_dispatched_tasks,
    reconcile_status: Callable[[], dict] = _production_reconciliation,
    max_waves: int = 10,
    recovery_action_files: Sequence[Path] = (),
) -> dict:
    recovery_queue = tuple(
        Path(path).expanduser().resolve() for path in recovery_action_files
    )
    if recovery_queue and pipeline_runner is not _run_global_dispatched_tasks:
        raise ValueError("production_rolling_recovery_runner_invalid")
    controller_handoff.assert_ready_for_start()
    try:
        with runtime_control.global_controller_lease():
            cache_maintenance = attachment_cache.prune_expired()
            historical_unknown_identities = (
                _local_unknown_quarantined_identities()
                if pipeline_runner is _run_global_dispatched_tasks
                else frozenset()
            )
            manifest = controller_handoff.begin(
                {
                    "capacity": capacity,
                    "adaptive": adaptive,
                    "max_capacity": max_capacity,
                    "model": codex_review_worker.MODEL,
                    "reasoning_effort": codex_review_worker.REASONING_EFFORT,
                    "prepare_capacity": 1,
                    "prepare_timeout_seconds": 300,
                    "review_timeout_seconds": 960,
                    "writer_capacity": 1,
                    "recovery_count": len(recovery_queue),
                }
            )
            children_reaped = True
            pending_recoveries = recovery_queue

            def tracked_pipeline_runner(
                count: int,
                review_capacity: int,
            ) -> process_pool.PoolResult[dict]:
                nonlocal children_reaped, pending_recoveries
                children_reaped = False
                if pipeline_runner is _run_global_dispatched_tasks:
                    wave_result = _run_global_dispatched_tasks(
                        count,
                        review_capacity,
                        adaptive=adaptive,
                        max_capacity=max_capacity,
                        historical_unknown_identities=(
                            historical_unknown_identities
                        ),
                        recovery_action_files=pending_recoveries,
                    )
                    pending_recoveries = ()
                else:
                    wave_result = pipeline_runner(count, review_capacity)
                children_reaped = wave_result.processes_reaped is True
                return wave_result

            try:
                result = _run_rolling_drain_locked(
                    capacity=capacity,
                    adaptive=adaptive,
                    max_capacity=max_capacity,
                    live_status=live_status,
                    pipeline_runner=tracked_pipeline_runner,
                    reconcile_status=reconcile_status,
                    max_waves=max_waves,
                    recovery_count=len(recovery_queue),
                )
                if result["processes_reaped"] is not True:
                    children_reaped = False
                    runtime_control.request_stop()
                    raise ValueError("production_controller_children_active")
            except Exception:
                controller_handoff.finish(
                    manifest,
                    status="failed",
                    children_reaped=children_reaped,
                    health="degraded",
                )
                raise
            handoff = controller_handoff.finish(
                manifest,
                status=(
                    "completed"
                    if result["status"]
                    in {"completed", "completed_with_quarantine"}
                    else "failed"
                ),
                children_reaped=True,
                health=(
                    "healthy" if result["status"] == "completed" else "degraded"
                ),
            )
            return {
                **result,
                "cache_maintenance": cache_maintenance,
                "controller_handoff": handoff,
            }
    except RuntimeError as error:
        if str(error) == "controller_already_active":
            raise ValueError("production_rolling_controller_active") from error
        raise


def _run_rolling_drain_locked(
    *,
    capacity: int,
    adaptive: bool = False,
    max_capacity: int = 12,
    live_status: LiveStatus = runtime_doctor.status,
    pipeline_runner: PipelineRunner = _run_global_dispatched_tasks,
    reconcile_status: Callable[[], dict] = _production_reconciliation,
    max_waves: int = 10,
    recovery_count: int = 0,
) -> dict:
    if (
        not 1 <= capacity <= 6
        or not isinstance(recovery_count, int)
        or isinstance(recovery_count, bool)
        or recovery_count < 0
    ):
        raise ValueError("production_rolling_capacity_invalid")
    if adaptive and (
        capacity not in concurrency_control.CAPACITY_STEPS
        or max_capacity not in concurrency_control.CAPACITY_STEPS
        or capacity > max_capacity
    ):
        raise ValueError("production_rolling_capacity_invalid")
    if runtime_control.stop_requested():
        raise ValueError("production_probe_stopped")
    started_at = time.monotonic()
    try:
        initial_pending, quarantined_pending, initial_actionable = (
            _rolling_snapshot(live_status, reconcile_status)
        )
    except ValueError as error:
        if str(error) == "production_rolling_reconciliation_invalid":
            raise
        raise ValueError("production_rolling_reconciliation_invalid") from error
    pending = initial_pending
    actionable = initial_actionable + recovery_count
    waves: list[dict] = []
    cache_maintenance_waves: list[dict] = []
    status = "completed"
    stop_reason: str | None = None
    healthy_windows = 0
    current_capacity = capacity
    last_decision = {
        "action": "hold",
        "next_capacity": current_capacity,
        "healthy_windows": 0,
        "reason": "fixed_capacity",
    }
    initial_quarantined = quarantined_pending
    while actionable:
        if len(waves) >= max_waves:
            runtime_control.request_stop()
            raise ValueError("production_rolling_wave_limit_reached")
        cache_maintenance_waves.append(attachment_cache.prune_expired())
        # A health window observes the continuous drain; it never caps claims.
        wave_size = actionable
        pipeline_result = (
            _run_global_dispatched_tasks(
                wave_size,
                current_capacity,
                adaptive=adaptive,
                max_capacity=max_capacity,
            )
            if pipeline_runner is _run_global_dispatched_tasks
            else pipeline_runner(wave_size, current_capacity)
        )
        wave = _rolling_wave_metrics(pipeline_result)
        wave["capacity"] = current_capacity
        waves.append(wave)
        if (
            wave["failed"]
            or wave["timed_out"]
            or wave["stopped"]
            or wave["completed"] != wave["requested"]
        ):
            runtime_control.request_stop()
            status = "failed"
            internal_stops = [
                decision
                for decision in wave.get("capacity_decisions", [])
                if decision.get("action") == "stop"
            ]
            if internal_stops:
                last_decision = internal_stops[-1]
                stop_reason = str(last_decision["reason"])
            else:
                stop_reason = (
                    "host_critical"
                    if wave["runtime_health"].get("status") == "critical"
                    else "technical_failure"
                )
                last_decision = {
                    "action": "stop",
                    "next_capacity": current_capacity,
                    "healthy_windows": 0,
                    "reason": stop_reason,
                }
            break
        try:
            pending, quarantined_pending, actionable = _rolling_snapshot(
                live_status,
                reconcile_status,
            )
        except ValueError:
            runtime_control.request_stop()
            raise
        if quarantined_pending > initial_quarantined:
            runtime_control.request_stop()
            status = "failed"
            stop_reason = "quarantine_increased"
            break
        if adaptive:
            internal_decisions = wave.get("capacity_decisions") or []
            if internal_decisions:
                last_decision = internal_decisions[-1]
                current_capacity = int(
                    wave.get("recommended_capacity") or current_capacity
                )
            else:
                last_decision = concurrency_control.decide_capacity(
                    current_capacity=current_capacity,
                    healthy_windows=healthy_windows,
                    wave={
                        "requested": wave["requested"],
                        "completed": wave["completed"],
                        "failed": wave["failed"],
                        "timed_out": wave["timed_out"],
                        "stopped": wave["stopped"],
                        "transient_error_events": wave[
                            "transient_error_events"
                        ],
                        "codex_event_status": wave["codex_events"]["status"],
                    },
                    host=wave["runtime_health"],
                )
            healthy_windows = int(last_decision["healthy_windows"])
            if last_decision["action"] == "stop":
                runtime_control.request_stop()
                status = "failed"
                stop_reason = str(last_decision["reason"])
                break
            if not internal_decisions:
                current_capacity = min(
                    int(last_decision["next_capacity"]),
                    max_capacity,
                )

    if status == "completed" and quarantined_pending:
        status = "completed_with_quarantine"

    business_results: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    usage_workers: Counter[str] = Counter()
    token_totals: Counter[str] = Counter()
    for wave in waves:
        business_results.update(wave["business_results"])
        error_counts.update(wave["error_counts"])
        usage = wave["token_usage"]
        for field in ("recorded_workers", "partial_workers", "unknown_workers"):
            usage_workers[field] += int(usage.get(field) or 0)
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            token_totals[field] += int(usage.get(field) or 0)
    completed = sum(wave["completed"] for wave in waves)
    codex_events = _merge_codex_event_summaries(
        [wave["codex_events"] for wave in waves]
    )
    transient_error_events = codex_events["counts"].get("error")
    if transient_error_events is None and codex_events["status"] == "recorded":
        transient_error_events = 0
    pipeline_wait_samples = [
        float(value)
        for wave in waves
        for value in wave.get("_pipeline_review_wait_samples", [])
    ]
    pipeline_writer_seconds = sum(
        float(wave.get("pipeline", {}).get("writer", {}).get("busy_seconds") or 0)
        for wave in waves
    )
    pipeline_active_review_sum = sum(
        int(wave.get("_pipeline_active_review_sum") or 0) for wave in waves
    )
    pipeline_review_capacity_sum = sum(
        int(wave.get("_pipeline_review_capacity_sum") or 0) for wave in waves
    )
    pipeline_elapsed = sum(float(wave["elapsed_seconds"]) for wave in waves)
    pipeline = {
        "review_done_to_write_start_seconds": _pipeline_percentiles(
            pipeline_wait_samples
        ),
        "writer": {
            "max_concurrency": max(
                (
                    int(wave.get("pipeline", {}).get("writer", {}).get(
                        "max_concurrency"
                    ) or 0)
                    for wave in waves
                ),
                default=0,
            ),
            "busy_seconds": round(pipeline_writer_seconds, 6),
            "busy_ratio": round(
                min(
                    1.0,
                    pipeline_writer_seconds / max(pipeline_elapsed, 1e-9),
                ),
                6,
            ),
        },
        "review_slots": {
            "samples": sum(
                int(wave.get("pipeline", {}).get("review_slots", {}).get(
                    "samples"
                ) or 0)
                for wave in waves
            ),
            "utilization": (
                round(
                    pipeline_active_review_sum
                    / pipeline_review_capacity_sum,
                    6,
                )
                if pipeline_review_capacity_sum
                else "unknown"
            ),
        },
        "write_queue": {
            "capacity": max(
                (
                    int(wave.get("pipeline", {}).get("write_queue", {}).get(
                        "capacity"
                    ) or 0)
                    for wave in waves
                ),
                default=0,
            ),
            "max_depth": max(
                (
                    int(wave.get("pipeline", {}).get("write_queue", {}).get(
                        "max_depth"
                    ) or 0)
                    for wave in waves
                ),
                default=0,
            ),
        },
    }
    processes_reaped = all(
        wave.get("processes_reaped") is True for wave in waves
    )
    return {
        "schema_version": 1,
        "kind": "preaudit_production_rolling_drain",
        "status": status,
        "scheduler": "rolling",
        "elastic": adaptive,
        "capacity": capacity,
        "model": codex_review_worker.MODEL,
        "reasoning_effort": codex_review_worker.REASONING_EFFORT,
        "start_capacity": capacity,
        "recommended_capacity": current_capacity,
        "last_capacity_decision": last_decision,
        "stop_reason": stop_reason,
        "wave_runtime_health": [wave["runtime_health"] for wave in waves],
        "initial_pending": initial_pending,
        "initial_actionable": initial_actionable,
        "final_pending": (
            pending
            if status in {"completed", "completed_with_quarantine"}
            else "unknown"
        ),
        "quarantined_pending": quarantined_pending,
        "waves": len(waves),
        "cache_maintenance_waves": cache_maintenance_waves,
        "completed": completed,
        "failed": sum(wave["failed"] for wave in waves),
        "timed_out": sum(wave["timed_out"] for wave in waves),
        "stopped": sum(wave["stopped"] for wave in waves),
        "peak_active": max((wave["peak_active"] for wave in waves), default=0),
        "elapsed_seconds": max(
            time.monotonic() - started_at,
            sum(wave["elapsed_seconds"] for wave in waves),
        ),
        "prepare_active_seconds": sum(
            wave["prepare_active_seconds"] for wave in waves
        ),
        "process_attempts": sum(wave["process_attempts"] for wave in waves),
        "process_retries": sum(wave["process_retries"] for wave in waves),
        "business_results": dict(sorted(business_results.items())),
        "error_counts": dict(sorted(error_counts.items())),
        "codex_events": codex_events,
        "transient_error_events": transient_error_events,
        "pipeline": pipeline,
        "processes_reaped": processes_reaped,
        "token_usage": {
            "status": (
                "recorded"
                if usage_workers["recorded_workers"] == completed
                else "partial"
                if usage_workers["recorded_workers"] or usage_workers["partial_workers"]
                else "unknown"
            ),
            **usage_workers,
            **token_totals,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finance-preaudit-concurrency-probe")
    parser.add_argument("--capacity", type=int, required=True)
    parser.add_argument("--confirm-write", choices=("YES",))
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--adaptive", action="store_true")
    parser.add_argument("--waves", type=int, default=6)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--rolling", action="store_true")
    parser.add_argument(
        "--recover-action-file",
        action="append",
        type=Path,
        default=[],
    )
    args = parser.parse_args(argv)
    try:
        if args.read_only:
            if args.drain or args.rolling or args.recover_action_file:
                raise ValueError("read_only_probe_scheduler_invalid")
            payload = (
                run_adaptive_read_only_probe(
                    start_capacity=args.capacity,
                    sample_size=args.samples,
                    max_waves=args.waves,
                )
                if args.adaptive
                else run_read_only_probe(
                    capacity=args.capacity,
                    sample_size=args.samples,
                )
            )
        elif args.confirm_write != "YES":
            raise ValueError("production_probe_write_confirmation_required")
        elif args.drain:
            raise ValueError("production_legacy_scheduler_removed")
        else:
            rolling_args = {
                "capacity": args.capacity,
                "adaptive": args.adaptive,
            }
            if args.recover_action_file:
                rolling_args["recovery_action_files"] = tuple(
                    args.recover_action_file
                )
            payload = run_rolling_drain(**rolling_args)
    except ValueError as error:
        payload = {
            "schema_version": 1,
            "kind": (
                "preaudit_read_only_concurrency_probe_failed"
                if args.read_only
                else "preaudit_production_concurrency_probe_failed"
            ),
            "error_code": str(error),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("status") != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
