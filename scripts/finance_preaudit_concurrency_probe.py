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
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import multiprocessing
from multiprocessing.connection import wait as wait_for_process
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from apps import json_artifact
from apps.oa_approval import attachment_cache, runtime_control, writer_outcomes
from apps.oa_finance_preaudit import (
    cli,
    codex_review_worker,
    concurrency_control,
    controller_handoff,
    flow_engine,
    item_failure,
    observability,
    prepare_failure,
    process_pool,
    production_pending,
    reconciliation,
    runtime_maintenance,
    session_alert,
)
from apps.oa_finance_preaudit import runtime_doctor


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT_PROJECT_SUFFIX = "-dev"
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
ReviewQuarantine = Callable[[Path, str], dict]
FailureRoundRecorder = Callable[[Path, str], dict]
ExecutorFactory = Callable[[int], object]


class ControllerChildrenActiveError(ValueError):
    def __init__(self, primary_error_code: str | None = None) -> None:
        super().__init__("production_controller_children_active")
        self.primary_error_code = primary_error_code


QUARANTINED_REASONS = {
    "codex_decision_invalid",
    "codex_review_failed",
    "material_collection_failed",
    *writer_outcomes.CONFIRMED_NO_WRITE_CODES,
}
RECOVERABLE_NO_WRITE_REASONS = {
    "write_authorization_invalid",
    *QUARANTINED_REASONS,
}
ITEM_LOCAL_QUARANTINE_REASONS = {
    "writer_submit_rejected",
    "codex_decision_invalid",
    "codex_review_failed",
    "codex_process_failed",
    "codex_process_incomplete",
    "material_collection_failed",
}
ITEM_LOCAL_REVIEW_FAILURE_REASONS = frozenset(
    {"codex_process_failed", "codex_process_incomplete", "codex_decision_invalid"}
)
# 同一轮里连续这么多单单据级审核进程失败时，停止本轮领取，避免把整批待办
# 全部推进隔离；阈值以内仍按既有规则继续处理。
REVIEW_FAILURE_CIRCUIT_BREAKER = 3
REVIEW_FAILURE_CIRCUIT_BREAKER_ERROR = "review_failure_circuit_breaker"


def _review_failure_streak_reached(streak: int) -> bool:
    """同一轮内连续的单据级审核失败是否达到熔断阈值。"""
    return streak >= REVIEW_FAILURE_CIRCUIT_BREAKER
ITEM_LOCAL_MATERIAL_FAILURE_DETAILS = (
    runtime_maintenance.ITEM_LOCAL_MATERIAL_FAILURE_DETAILS
)
SESSION_BLOCKING_FAILURE_CODES = flow_engine.SESSION_BLOCKING_FAILURE_CODES
ROLLING_WORKFLOW_KEYS = frozenset(
    {
        "travel_expense",
        "business_entertainment",
        "business_support_fee",
        "small_marketing_fee",
        "tpm_small_marketing_fee",
        "general_expense",
    }
)
_OA_TIMEZONE = ZoneInfo("Asia/Shanghai")
_SESSION_ALERT_PRIORITY = (
    "oa_execution_identity_login_failed",
    "oa_session_expired",
    "rpa_cdp_timeout",
    "rpa_cdp_unavailable",
    "rpa_session_invalid",
    "rpa_session_missing",
)


def _session_error_code(error: Exception) -> str:
    current: BaseException | None = error
    seen: set[int] = set()
    while isinstance(current, Exception) and id(current) not in seen:
        seen.add(id(current))
        code = flow_engine.session_blocking_failure_code(current)
        if code:
            return code
        current = current.__cause__ or current.__context__
    return ""


def _received_at_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("production_pending_received_at_invalid") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_OA_TIMEZONE)
    return parsed.astimezone(timezone.utc)


def _pending_identity_key(request_id: str, received_at: str) -> str:
    return hashlib.sha256(
        f"{request_id}\x1f{received_at}".encode("utf-8")
    ).hexdigest()


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("production_probe_state_invalid") from error
    if not isinstance(value, dict):
        raise ValueError("production_probe_state_invalid")
    return value


def _file_fingerprint(path: Path) -> str:
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ValueError("production_probe_state_invalid") from error
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _persist_write_wait_evidence(
    action_file: Path,
    *,
    ready_at: float,
    write_started_at: float,
    queue_depth: int,
    attribution: dict,
    event_suffix: int,
) -> bool:
    try:
        state = _read_object(action_file.with_name("state.json"))
        request_id = str(state["request_id"])
        received_at = str(state["received_at"])
        attempt = state["attempt"]
        workflow_key = str(state["workflow_key"])
        if (
            not request_id
            or not received_at
            or not workflow_key
            or not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 1
            or not isinstance(event_suffix, int)
            or isinstance(event_suffix, bool)
            or event_suffix < 1
        ):
            raise ValueError("pipeline_metrics_invalid")
        item_key = observability.attempt_key(
            request_id,
            received_at,
            attempt,
        )
        metrics_path = action_file.with_name("pipeline-metrics.json")
        if metrics_path.is_symlink():
            raise ValueError("pipeline_metrics_invalid")
        persisted: dict
        if metrics_path.exists():
            if not metrics_path.is_file():
                raise ValueError("pipeline_metrics_invalid")
            existing = _read_object(metrics_path)
            existing_suffix = existing.get("event_suffix")
            if (
                not isinstance(existing_suffix, int)
                or isinstance(existing_suffix, bool)
                or existing_suffix < 1
                or not _write_wait_evidence_matches(
                    existing,
                    item_key=item_key,
                    workflow_key=workflow_key,
                    attempt=attempt,
                    event_suffix=existing_suffix,
                )
            ):
                raise ValueError("pipeline_metrics_invalid")
            existing_events = _write_wait_events(existing)
            events_complete = _write_wait_events_complete(
                action_file.with_name("observability.json"),
                existing_events,
            )
            if not events_complete:
                persisted = existing
            else:
                if not events_complete:
                    raise ValueError("pipeline_metrics_invalid")
                persisted = _new_write_wait_evidence(
                    item_key=item_key,
                    workflow_key=workflow_key,
                    attempt=attempt,
                    ready_at=ready_at,
                    write_started_at=write_started_at,
                    queue_depth=queue_depth,
                    attribution=attribution,
                    event_suffix=max(event_suffix, existing_suffix + 1),
                )
                json_artifact.write_json_atomic(
                    metrics_path,
                    persisted,
                    indent=2,
                )
        else:
            persisted = _new_write_wait_evidence(
                item_key=item_key,
                workflow_key=workflow_key,
                attempt=attempt,
                ready_at=ready_at,
                write_started_at=write_started_at,
                queue_depth=queue_depth,
                attribution=attribution,
                event_suffix=event_suffix,
            )
            json_artifact.write_json_atomic(metrics_path, persisted, indent=2)
        if not _write_wait_evidence_matches(
            persisted,
            item_key=item_key,
            workflow_key=workflow_key,
            attempt=attempt,
            event_suffix=int(persisted["event_suffix"]),
        ):
            raise ValueError("pipeline_metrics_invalid")
        for event in _write_wait_events(persisted):
            observability.record_event(
                action_file.with_name("observability.json"),
                event,
            )
    except (KeyError, OSError, TypeError, ValueError):
        return False
    return True


def _new_write_wait_evidence(
    *,
    item_key: str,
    workflow_key: str,
    attempt: int,
    ready_at: float,
    write_started_at: float,
    queue_depth: int,
    attribution: dict,
    event_suffix: int,
) -> dict:
    if (
        not math.isfinite(ready_at)
        or not math.isfinite(write_started_at)
        or write_started_at < ready_at
        or not isinstance(queue_depth, int)
        or isinstance(queue_depth, bool)
        or queue_depth < 0
        or not _valid_write_wait_attribution(attribution)
    ):
        raise ValueError("pipeline_metrics_invalid")
    write_started_utc = datetime.now(timezone.utc)
    ready_utc = write_started_utc - timedelta(
        seconds=write_started_at - ready_at
    )
    return {
        "schema_version": 1,
        "kind": "preaudit_pipeline_metrics",
        "status": "exact",
        "item_key": item_key,
        "workflow_key": workflow_key,
        "attempt": attempt,
        "event_suffix": event_suffix,
        "write_ready_at": ready_utc.isoformat(),
        "write_start_at": write_started_utc.isoformat(),
        "write_queue_depth_at_enqueue": queue_depth,
        "write_wait_attribution": attribution,
    }


def _write_wait_evidence_matches(
    payload: dict,
    *,
    item_key: str,
    workflow_key: str,
    attempt: int,
    event_suffix: int,
) -> bool:
    expected_fields = {
        "schema_version",
        "kind",
        "status",
        "item_key",
        "workflow_key",
        "attempt",
        "event_suffix",
        "write_ready_at",
        "write_start_at",
        "write_queue_depth_at_enqueue",
        "write_wait_attribution",
    }
    try:
        ready = datetime.fromisoformat(
            str(payload["write_ready_at"]).replace("Z", "+00:00")
        )
        started = datetime.fromisoformat(
            str(payload["write_start_at"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError):
        return False
    return (
        set(payload) == expected_fields
        and isinstance(payload.get("schema_version"), int)
        and not isinstance(payload.get("schema_version"), bool)
        and payload["schema_version"] == 1
        and payload.get("kind") == "preaudit_pipeline_metrics"
        and payload.get("status") == "exact"
        and payload.get("item_key") == item_key
        and payload.get("workflow_key") == workflow_key
        and isinstance(payload.get("attempt"), int)
        and not isinstance(payload.get("attempt"), bool)
        and payload["attempt"] == attempt
        and isinstance(payload.get("event_suffix"), int)
        and not isinstance(payload.get("event_suffix"), bool)
        and payload["event_suffix"] == event_suffix
        and ready.tzinfo is not None
        and started.tzinfo is not None
        and ready <= started
        and isinstance(payload.get("write_queue_depth_at_enqueue"), int)
        and not isinstance(
            payload.get("write_queue_depth_at_enqueue"),
            bool,
        )
        and payload["write_queue_depth_at_enqueue"] >= 0
        and _valid_write_wait_attribution(
            payload.get("write_wait_attribution")
        )
    )


def _write_wait_events(payload: dict) -> tuple[dict, dict]:
    ready_utc = datetime.fromisoformat(
        str(payload["write_ready_at"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    write_started_utc = datetime.fromisoformat(
        str(payload["write_start_at"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    suffix = int(payload["event_suffix"])
    shared = {
        "item_key": str(payload["item_key"]),
        "workflow_key": str(payload["workflow_key"]),
        "phase": "wait",
        "active_seconds": 0,
        "result": "success",
        "model": None,
        "token_usage": {"status": "unknown"},
    }
    total_seconds = max(
        0,
        int((write_started_utc - ready_utc).total_seconds()),
    )
    return (
        {
            **shared,
            "event_id": f"04-wait-ready-{suffix}",
            "occurred_at": ready_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "wall_seconds": 0,
            "queue_wait_seconds": 0,
        },
        {
            **shared,
            "event_id": f"04-wait-duration-{suffix}",
            "occurred_at": write_started_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "wall_seconds": total_seconds,
            "queue_wait_seconds": total_seconds,
        },
    )


def _write_wait_events_complete(path: Path, expected: tuple[dict, dict]) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        payload = _read_object(path)
        events = payload["events"]
    except (KeyError, OSError, TypeError, ValueError):
        return False
    return isinstance(events, list) and all(event in events for event in expected)


def _valid_write_wait_attribution(value: object) -> bool:
    if value == {"status": "unknown"}:
        return True
    fields = {
        "status",
        "total_seconds",
        "writer_busy_seconds",
        "fifo_head_of_line_seconds",
        "controller_scheduling_seconds",
    }
    if not isinstance(value, dict) or set(value) != fields:
        return False
    numbers = [value.get(field) for field in fields - {"status"}]
    return value.get("status") == "exact" and all(
        isinstance(number, (int, float))
        and not isinstance(number, bool)
        and math.isfinite(float(number))
        and number >= 0
        for number in numbers
    ) and math.isclose(
        float(value["total_seconds"]),
        float(value["writer_busy_seconds"])
        + float(value["fifo_head_of_line_seconds"])
        + float(value["controller_scheduling_seconds"]),
        abs_tol=1e-6,
    )


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


def _material_failure_action_matches_selection(
    action_path: Path,
    action: dict,
    state: dict,
    selected: dict,
    workflow_key: str,
    *,
    allowed_failure_details: frozenset[str],
    forbidden_names: tuple[str, ...],
    expected_reason: str = "material_collection_failed",
    project_root: Path = PROJECT_ROOT,
) -> bool:
    request_id = str(selected.get("requestId") or "").strip()
    received_at = str(selected.get("received_at") or "").strip()
    attempt = state.get("attempt")
    reason = str(state.get("reason") or "")
    detail_code = str(state.get("failure_detail_code") or "")
    action_detail_code = str(action.get("failure_detail_code") or "")
    if (
        not request_id
        or not received_at
        or not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or attempt < 1
        or reason != expected_reason
        or detail_code not in allowed_failure_details
        or action_detail_code != detail_code
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
        "action": "needs_admin",
        "execution_state": "needs_admin",
        "business_state": "not_available",
        "write_state": "not_requested",
        "workflow_key": workflow_key,
        "reason": reason,
        "case_key": case_key,
        "attempt": attempt,
        "action_file": str(expected_action),
        "state_file": str(expected_state),
        **(
            {"failure_detail_code": detail_code}
            if detail_code
            else {}
        ),
    }
    attempt_root = expected_action.parent
    forbidden = [
        attempt_root / "writer-boundary.json",
        attempt_root / "execution-receipt.json",
        *(attempt_root / name for name in forbidden_names),
    ]
    return (
        action_path == expected_action
        and state.get("request_id") == request_id
        and state.get("received_at") == received_at
        and all(action.get(key) == value for key, value in shared.items())
        and all(state.get(key) == value for key, value in shared.items())
        and not any(path.is_symlink() or path.exists() for path in forbidden)
    )


def _quarantined_action_matches_selection(
    action_path: Path,
    action: dict,
    state: dict,
    selected: dict,
    workflow_key: str,
    *,
    project_root: Path = PROJECT_ROOT,
) -> bool:
    if state.get("reason") == "writer_submit_rejected":
        expected_root = Path(project_root).expanduser().resolve() / "runtime" / "finance_preaudit_flows"
        return (
            action_path == expected_root / workflow_key / "cases"
            / str(state.get("case_key")) / f"attempt-{state.get('attempt')}" / "action.json"
            and state.get("request_id") == str(selected.get("requestId") or "").strip()
            and state.get("received_at") == str(selected.get("received_at") or "").strip()
            and item_failure.isolated(action_path.parent, state)
        )
    if state.get("reason") in {
        "codex_decision_invalid",
        "codex_review_failed",
    }:
        trusted = _material_failure_action_matches_selection(
            action_path,
            action,
            state,
            selected,
            workflow_key,
            allowed_failure_details=frozenset({""}),
            forbidden_names=(
                "review-decision.json",
                "remark.txt",
                "submission-envelope.json",
            ),
            expected_reason=str(state["reason"]),
            project_root=project_root,
        )
        if state.get("reason") != "codex_review_failed":
            return trusted
        receipt_path = action_path.with_name("review-failure.json")
        try:
            receipt_fingerprint = _file_fingerprint(receipt_path)
        except ValueError:
            return False
        return (
            trusted
            and not receipt_path.is_symlink()
            and action.get("review_failure_fingerprint")
            == state.get("review_failure_fingerprint")
            == receipt_fingerprint
        )
    return _material_failure_action_matches_selection(
        action_path,
        action,
        state,
        selected,
        workflow_key,
        allowed_failure_details=ITEM_LOCAL_MATERIAL_FAILURE_DETAILS,
        forbidden_names=(
            "review-decision.json",
            "remark.txt",
            "submission-envelope.json",
        ),
        project_root=project_root,
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
            runtime_control.request_cycle_stop()
            raise ValueError("production_probe_renew_failed") from error
        if (
            current is None
            or current.get("action") != "review_required"
            or current.get("action_file") != str(action_file)
            or current.get("write_state") != "authorized"
        ):
            runtime_control.request_cycle_stop()
            raise ValueError("production_probe_renew_failed")
        renewed.append(action_file)
    return renewed


def _latest_local_states() -> list[dict]:
    runtime_root = PROJECT_ROOT / "runtime"
    flow_root = runtime_root / "finance_preaudit_flows"
    if runtime_root.is_symlink() or flow_root.is_symlink():
        raise ValueError("runtime_reconciliation_invalid")
    if not flow_root.exists():
        if runtime_root.exists() and not runtime_root.is_dir():
            raise ValueError("runtime_reconciliation_invalid")
        return []
    return reconciliation.latest_states(flow_root)


def _historical_write_authorization_failures() -> frozenset[Path]:
    flow_root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    historical: set[Path] = set()
    for state in _latest_local_states():
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
    identities: set[tuple[str, str]] = set()
    for state in _latest_local_states():
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


def _include_isolated_development_items() -> bool:
    """Development clones must keep isolated items in the diagnostic queue."""
    return PROJECT_ROOT.name.endswith(DEVELOPMENT_PROJECT_SUFFIX)


def _local_unknown_quarantined_identities() -> frozenset[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    for state in _latest_local_states():
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
    for state in _latest_local_states():
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
            or state.get("reason") not in RECOVERABLE_NO_WRITE_REASONS
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
    global_selection = False
    assigned_identities: set[tuple[str, str]] = set()
    historical_unknown_identities: set[tuple[str, str]] = set()
    recovery_action_files: tuple[Path, ...] = ()
    committed_received_at: datetime | None = None
    committed_request_id: str | None = None
    known_late_visible_keys: set[str] = set()
    admission_cutoff: datetime | None = None
    allowed_workflows = ROLLING_WORKFLOW_KEYS

    def failed(
        stage: str,
        *,
        error_code: str = "production_probe_prepare_failed",
        reason_code: str | None = None,
        error: BaseException | None = None,
        stop_action: str | None = None,
        workflow_key: str | None = None,
        action_file: Path | None = None,
    ) -> dict:
        # Every generic prepare failure keeps its stable code and adds one
        # bounded, redacted diagnostic so the controller receipt stays usable.
        return {
            "action": "failed",
            "error_code": error_code,
            "prepare_failure": prepare_failure.describe(
                stage,
                error_code=error_code,
                reason_code=reason_code or error_code,
                error=error,
                stop_action=stop_action,
                workflow_key=workflow_key,
                action_file=(
                    str(action_file) if action_file is not None else None
                ),
            ),
        }

    if isinstance(selection, str):
        workflow_key = selection
    elif isinstance(selection, dict) and selection.get("workflow_key") == "*":
        global_selection = True
        workflow_key = "*"
        raw_assigned = selection.get("assigned_identities")
        raw_historical_unknown = selection.get(
            "historical_unknown_identities",
            [],
        )
        raw_recovery_actions = selection.get("recover_action_files", [])
        raw_committed_received_at = selection.get("committed_received_at")
        raw_committed_request_id = selection.get("committed_request_id")
        raw_late_visible_keys = selection.get("known_late_visible_keys", [])
        raw_admission_cutoff = selection.get("admission_cutoff")
        raw_allowed_workflows = selection.get(
            "allowed_workflows",
            sorted(ROLLING_WORKFLOW_KEYS),
        )
        if not isinstance(raw_assigned, list) or not isinstance(
            raw_historical_unknown,
            list,
        ) or not isinstance(raw_recovery_actions, list) or not isinstance(
            raw_late_visible_keys,
            list,
        ) or not isinstance(raw_allowed_workflows, list) or not raw_allowed_workflows or (
            len(set(raw_allowed_workflows)) != len(raw_allowed_workflows)
        ) or any(
            not isinstance(value, str) or value not in ROLLING_WORKFLOW_KEYS
            for value in raw_allowed_workflows
        ) or any(
            not isinstance(path, str) or not path
            for path in raw_recovery_actions
        ) or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in raw_late_visible_keys
        ):
            return failed("selection_contract")
        allowed_workflows = frozenset(raw_allowed_workflows)
        for identity in raw_assigned:
            if (
                not isinstance(identity, list)
                or len(identity) != 2
                or not all(isinstance(value, str) and value for value in identity)
            ):
                return failed("selection_contract")
            assigned_identities.add((identity[0], identity[1]))
        for identity in raw_historical_unknown:
            if (
                not isinstance(identity, list)
                or len(identity) != 2
                or not all(isinstance(value, str) and value for value in identity)
            ):
                return failed("selection_contract")
            historical_unknown_identities.add((identity[0], identity[1]))
        recovery_action_files = tuple(Path(path) for path in raw_recovery_actions)
        known_late_visible_keys.update(raw_late_visible_keys)
        try:
            if raw_committed_received_at is not None:
                if (
                    not isinstance(raw_committed_received_at, str)
                    or not isinstance(raw_committed_request_id, str)
                    or not raw_committed_request_id
                ):
                    raise ValueError
                committed_received_at = _received_at_utc(
                    raw_committed_received_at
                )
                committed_request_id = raw_committed_request_id
            elif raw_committed_request_id is not None:
                raise ValueError
            if raw_admission_cutoff is not None:
                if not isinstance(raw_admission_cutoff, str):
                    raise ValueError
                admission_cutoff = _received_at_utc(raw_admission_cutoff)
        except ValueError:
            return failed("selection_contract")
    else:
        return failed("selection_contract")
    if runtime_control.stop_requested():
        return {"action": "failed", "error_code": "production_probe_stopped"}
    try:
        historical_authorization_failures = (
            _historical_write_authorization_failures()
        )
        quarantine_budget = _local_quarantine_count() + 1
    except ValueError as error:
        runtime_control.request_stop("production_probe_prepare_failed")
        return failed("quarantine_probe", error=error, stop_action="stop")
    quarantined = 0
    newly_late_visible_keys: set[str] = set()
    late_visible_pending_count = 0
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
            if (
                admission_cutoff is not None
                and datetime.now(timezone.utc) > admission_cutoff
            ):
                return {"action": "admission_closed"}
            try:
                live_candidates = production_pending.actionable_fifo(
                    cli._live_pending_identities(),
                    excluded_identities=(
                        (
                            frozenset()
                            if _include_isolated_development_items()
                            else _local_quarantined_identities()
                        )
                        | historical_unknown_identities
                        | assigned_identities
                    ),
                    allowed_workflows=allowed_workflows,
                )
            except Exception as error:
                runtime_control.request_cycle_stop()
                return failed(
                    "live_pending_read",
                    error_code=(
                        flow_engine.session_blocking_failure_code(error)
                        or "production_probe_prepare_failed"
                    ),
                    error=error,
                    stop_action="cycle_stop",
                    workflow_key=selected_workflow,
                )
            eligible_live_candidates: list[dict] = []
            for candidate in live_candidates:
                request_id = str(candidate.get("requestId") or "")
                received_at = str(candidate.get("received_at") or "")
                identity_key = _pending_identity_key(request_id, received_at)
                if identity_key in known_late_visible_keys:
                    late_visible_pending_count += 1
                    continue
                try:
                    candidate_received_at = _received_at_utc(received_at)
                except ValueError as error:
                    runtime_control.request_stop(
                        "production_probe_prepare_failed"
                    )
                    return failed(
                        "identity_contract",
                        error=error,
                        stop_action="stop",
                        workflow_key=str(candidate.get("flow_key") or ""),
                    )
                if committed_received_at is not None and (
                    candidate_received_at,
                    request_id,
                ) < (committed_received_at, str(committed_request_id)):
                    newly_late_visible_keys.add(identity_key)
                    late_visible_pending_count += 1
                    continue
                eligible_live_candidates.append(candidate)
            live_candidates = eligible_live_candidates
            if (
                admission_cutoff is not None
                and datetime.now(timezone.utc) > admission_cutoff
            ):
                return {"action": "admission_closed"}
            try:
                recovery_candidates = production_pending.actionable_fifo(
                    _recovery_candidates(
                        recovery_action_files,
                        assigned_identities=assigned_identities,
                    ),
                    allowed_workflows=allowed_workflows,
                )
            except (ValueError, OSError):
                runtime_control.request_stop("production_probe_prepare_failed")
                return {
                    "action": "failed",
                    "error_code": "production_probe_recovery_failed",
                }
            candidates = production_pending.actionable_fifo(
                [*live_candidates, *recovery_candidates],
                allowed_workflows=allowed_workflows,
            )
            if not candidates:
                return (
                    {
                        "action": "late_visible",
                        "late_visible_keys": sorted(newly_late_visible_keys),
                        "late_visible_pending_count": (
                            late_visible_pending_count
                        ),
                    }
                    if newly_late_visible_keys
                    else {
                        "action": "no_action",
                        "late_visible_pending_count": (
                            late_visible_pending_count
                        ),
                    }
                )
            selected = candidates[0]
            selected_workflow = str(selected.get("flow_key") or "")
            if selected.get("recovery_action_file") is not None:
                recovery_action_file = Path(selected["recovery_action_file"])
            if (
                admission_cutoff is not None
                and datetime.now(timezone.utc) > admission_cutoff
            ):
                return {"action": "admission_closed"}
        try:
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
        except Exception as error:
            runtime_control.request_cycle_stop()
            return failed(
                "engine_build",
                error_code=(
                    flow_engine.session_blocking_failure_code(error)
                    or "production_probe_prepare_failed"
                ),
                error=error,
                stop_action="cycle_stop",
                workflow_key=selected_workflow,
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
                session_failure_code = (
                    flow_engine.session_blocking_failure_code(error)
                )
                if recovery_action_file is not None and not session_failure_code:
                    runtime_control.request_stop("production_probe_prepare_failed")
                    stop_action = "stop"
                else:
                    runtime_control.request_cycle_stop()
                    stop_action = "cycle_stop"
                return failed(
                    "next_action",
                    error_code=(
                        session_failure_code
                        or (
                            "production_probe_recovery_failed"
                            if recovery_action_file is not None
                            else "production_probe_prepare_failed"
                        )
                    ),
                    error=error,
                    stop_action=stop_action,
                    workflow_key=selected_workflow,
                    action_file=recovery_action_file,
                )
            except Exception as error:
                session_failure_code = (
                    flow_engine.session_blocking_failure_code(error)
                )
                if recovery_action_file is not None and not session_failure_code:
                    runtime_control.request_stop("production_probe_prepare_failed")
                    stop_action = "stop"
                else:
                    runtime_control.request_cycle_stop()
                    stop_action = "cycle_stop"
                return failed(
                    "next_action",
                    error_code=(
                        session_failure_code
                        or (
                            "production_probe_recovery_failed"
                            if recovery_action_file is not None
                            else "production_probe_prepare_failed"
                        )
                    ),
                    error=error,
                    stop_action=stop_action,
                    workflow_key=selected_workflow,
                    action_file=recovery_action_file,
                )
            break
        if runtime_control.stop_requested():
            return {
                "action": "failed",
                "error_code": "production_probe_stopped",
            }
        if recovery_action_file is not None and action.get("action") != "review_required":
            runtime_control.request_stop("production_probe_prepare_failed")
            return failed(
                "unexpected_action",
                error_code="production_probe_recovery_failed",
                stop_action="stop",
                workflow_key=selected_workflow,
                action_file=recovery_action_file,
            )
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
                **(
                    {"late_visible_keys": sorted(newly_late_visible_keys)}
                    if newly_late_visible_keys
                    else {}
                ),
                **(
                    {"late_visible_pending_count": late_visible_pending_count}
                    if global_selection and late_visible_pending_count
                    else {}
                ),
            }
        if (
            action.get("action") == "needs_admin"
            and (
                action.get("reason") in QUARANTINED_REASONS
            )
        ):
            session_failure_code = str(
                action.get("failure_detail_code") or ""
            )
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
                except (KeyError, OSError, ValueError):
                    pass
                if session_failure_code in SESSION_BLOCKING_FAILURE_CODES:
                    if not _material_failure_action_matches_selection(
                        action_path,
                        persisted_action,
                        persisted,
                        selected,
                        selected_workflow,
                        allowed_failure_details=(
                            SESSION_BLOCKING_FAILURE_CODES
                        ),
                        forbidden_names=(
                            "material.json",
                            "review-packet.json",
                            "review-decision.json",
                            "remark.txt",
                            "submission-envelope.json",
                        ),
                        project_root=PROJECT_ROOT,
                    ):
                        runtime_control.request_stop(
                            "production_probe_prepare_failed"
                        )
                        return {
                            "action": "failed",
                            "error_code": "production_probe_fifo_mismatch",
                        }
                    runtime_control.request_cycle_stop()
                    return {
                        "action": "failed",
                        "error_code": session_failure_code,
                    }
                if not _quarantined_action_matches_selection(
                    action_path,
                    persisted_action,
                    persisted,
                    selected,
                    selected_workflow,
                    project_root=PROJECT_ROOT,
                ):
                    runtime_control.request_stop(
                        "production_probe_prepare_failed"
                    )
                    return {
                        "action": "failed",
                        "error_code": "production_probe_fifo_mismatch",
                    }
                return {
                    "action": "quarantined",
                    "error_code": str(action["reason"]),
                    "write_state": "not_requested",
                    **(
                        {"late_visible_keys": sorted(newly_late_visible_keys)}
                        if newly_late_visible_keys
                        else {}
                    ),
                    **(
                        {
                            "late_visible_pending_count": (
                                late_visible_pending_count
                            )
                        }
                        if global_selection and late_visible_pending_count
                        else {}
                    ),
                }
            if session_failure_code in SESSION_BLOCKING_FAILURE_CODES:
                runtime_control.request_cycle_stop()
                return {
                    "action": "failed",
                    "error_code": session_failure_code,
                }
            quarantined += 1
            try:
                current_quarantine_count = _local_quarantine_count()
            except ValueError as error:
                runtime_control.request_stop("production_probe_prepare_failed")
                return failed(
                    "quarantine_probe",
                    error=error,
                    stop_action="stop",
                    workflow_key=selected_workflow,
                )
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
            stop_action = "stop"
        except ValueError:
            runtime_control.request_cycle_stop()
            stop_action = "cycle_stop"
        return {
            "action": str(action.get("action") or "failed"),
            "error_code": error_code,
            "prepare_failure": prepare_failure.describe(
                "unexpected_action",
                error_code=(
                    error_code
                    if prepare_failure.is_code(error_code)
                    else "production_probe_prepare_failed"
                ),
                stop_action=stop_action,
                workflow_key=selected_workflow,
            ),
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
    rate_limit_events = codex_events["counts"].get("rate_limit_signal")
    if rate_limit_events is None and codex_events["status"] == "recorded":
        rate_limit_events = 0
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
        "rate_limit_events": rate_limit_events,
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
    start_capacity: int = 2,
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
                "rate_limit_events": wave.get(
                    "rate_limit_events",
                    0
                    if isinstance(wave.get("codex_events"), dict)
                    and wave["codex_events"].get("status") == "recorded"
                    else None,
                ),
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
    rate_limit_events = codex_events["counts"].get("rate_limit_signal")
    if rate_limit_events is None and codex_events["status"] == "recorded":
        rate_limit_events = 0
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
        "rate_limit_events": rate_limit_events,
        "production_write": False,
    }


def _review_only(task: dict) -> dict:
    return codex_review_worker.review(task)


def _process_executor(capacity: int) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(
        max_workers=capacity,
        mp_context=multiprocessing.get_context("spawn"),
    )


def _initialize_preparation_worker() -> None:
    os.setsid()
    parent = multiprocessing.parent_process()
    if parent is None:
        raise RuntimeError("preparation_parent_missing")

    def reap_after_parent_exit() -> None:
        # The sentinel identifies the original parent without PID reuse races.
        # Only this read-only preparation group is owned here, never the Writer.
        wait_for_process([parent.sentinel])
        if os.getpgrp() == os.getpid():
            os.killpg(os.getpgrp(), signal.SIGKILL)

    threading.Thread(
        target=reap_after_parent_exit,
        name="oa-preparation-parent-watch",
        daemon=True,
    ).start()


def _preparation_executor() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_initialize_preparation_worker,
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
    return _processes_reaped(processes, timeout_seconds=5)


def _processes_reaped(
    processes: Sequence[object],
    *,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        alive = [process for process in processes if process.is_alive()]
        if not alive:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        for process in alive:
            process.join(timeout=min(0.1, remaining))


_PREPARE_FAILURE_FIELDS = (
    "stage",
    "error_code",
    "reason_code",
    "error_class_name",
    "last_frame_name",
    "stop_action",
    "workflow_key",
    "case_key",
    "attempt",
    "action_file",
)


def _prepare_failure_directory(action_file: Path | None) -> Path:
    flow_root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    if action_file is not None:
        try:
            if (
                action_file.is_absolute()
                and not action_file.is_symlink()
                and action_file.is_relative_to(flow_root)
                and action_file.parent.is_dir()
                and not action_file.parent.is_symlink()
            ):
                return action_file.parent
        except (OSError, ValueError):
            pass
    return PROJECT_ROOT / "runtime" / "control"


def _prepare_failure_item(action_file: Path) -> dict:
    """Derive the item identity encoded in an attempt action path."""
    flow_root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    try:
        relative = action_file.relative_to(flow_root)
    except ValueError:
        return {}
    parts = relative.parts
    if len(parts) != 5 or parts[1] != "cases" or parts[4] != "action.json":
        return {}
    attempt_text = parts[3].removeprefix("attempt-")
    if parts[3] == attempt_text or not attempt_text.isdigit():
        return {}
    return {
        "workflow_key": parts[0],
        "case_key": parts[2],
        "attempt": int(attempt_text),
    }


def _record_prepare_failure(
    diagnostic: object,
    *,
    action_file: Path | None = None,
    run_id: str | None = None,
    controller_instance_id: str | None = None,
) -> Path | None:
    """Persist one bounded prepare-failure receipt.

    Diagnosis must never change control flow: unknown fields and any write
    failure are dropped, and the caller keeps its original stop behaviour.
    """
    if not isinstance(diagnostic, dict):
        return None
    request = {
        key: diagnostic[key]
        for key in _PREPARE_FAILURE_FIELDS
        if key in diagnostic
    }
    if action_file is not None:
        request["action_file"] = str(action_file)
    raw_action_file = request.get("action_file")
    try:
        target = _prepare_failure_directory(
            Path(raw_action_file) if isinstance(raw_action_file, str) else None
        )
        return prepare_failure.record(
            target,
            create=True,
            run_id=run_id,
            controller_instance_id=controller_instance_id,
            **request,
        )
    except (OSError, TypeError, ValueError):
        return None


def _run_dispatched_tasks(
    count: int,
    capacity: int,
    *,
    project_root: Path = PROJECT_ROOT,
    renew_actions: RenewActions = _renew_actions,
    prepare_action: PrepareAction | None = None,
    review_worker: ReviewWorker = _review_only,
    complete_review: ReviewCompleter = codex_review_worker.complete_review_attempt,
    quarantine_review: ReviewQuarantine = (
        codex_review_worker.quarantine_review_failure
    ),
    record_failure_round: FailureRoundRecorder | None = None,
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
    committed_received_at: str | None = None,
    committed_request_id: str | None = None,
    known_late_visible_keys: frozenset[str] = frozenset(),
    admission_cutoff: str | None = None,
    continuous_discovery: bool = False,
    allowed_workflows: frozenset[str] = ROLLING_WORKFLOW_KEYS,
    run_id: str | None = None,
    controller_instance_id: str | None = None,
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
        or not allowed_workflows
        or not allowed_workflows.issubset(ROLLING_WORKFLOW_KEYS)
    ):
        raise ValueError("production_dispatcher_watchdog_invalid")
    if adaptive and (
        capacity not in concurrency_control.CAPACITY_STEPS
        or maximum_capacity not in concurrency_control.CAPACITY_STEPS
    ):
        raise ValueError("production_dispatcher_watchdog_invalid")
    try:
        if (committed_received_at is None) != (committed_request_id is None):
            raise ValueError
        committed_watermark = (
            (_received_at_utc(committed_received_at), str(committed_request_id))
            if committed_received_at is not None
            else None
        )
        admission_deadline = (
            _received_at_utc(admission_cutoff)
            if admission_cutoff is not None
            else None
        )
    except ValueError as error:
        raise ValueError("production_dispatcher_watchdog_invalid") from error
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in known_late_visible_keys
    ):
        raise ValueError("production_dispatcher_watchdog_invalid")
    started_at = time.monotonic()
    root = Path(project_root).expanduser().resolve()
    outcomes: list[process_pool.TaskOutcome[dict] | None] = [None] * count
    assigned: set[Path] = set()
    assigned_identities: dict[Path, tuple[str, str]] = {}
    assigned_order_keys: dict[int, tuple[datetime, str]] = {}
    pending_write_indices: set[int] = set()
    futures: dict[object, tuple[int, float, float, Path]] = {}
    pending_reviews: dict[int, tuple[float, Path, bool]] = {}
    write_ready: dict[
        int,
        tuple[float, Path, dict, float, int, float | None],
    ] = {}
    writer: (
        tuple[
            Future[dict],
            int,
            float,
            Path,
            dict,
            float,
            float,
            int,
            dict,
        ]
        | None
    ) = None
    writer_busy_intervals: list[tuple[float, float]] = []
    review_histories: dict[int, list[dict]] = {}
    decision_retries: set[int] = set()
    skipped_write_indices: set[int] = set()
    initial_late_visible_keys = set(known_late_visible_keys)
    late_visible_keys = set(initial_late_visible_keys)
    late_visible_pending_count = 0
    committed_order_key = committed_watermark
    next_index = 0
    next_write_index = 0
    current_review_capacity = capacity
    adaptive_healthy_windows = 0
    adaptive_window_target = capacity * 2
    adaptive_window_values: list[dict] = []
    adaptive_window_hosts: list[dict] = []
    capacity_decisions: list[dict] = []
    adaptive_pressure_active = False
    resource_paused = False
    started = 0
    peak_active = 0
    halted = False
    consecutive_review_failures = 0
    review_retry_diagnostics = {
        "item_local_failure": 0,
        "bounded": 0,
        "unbounded": 0,
        "recorder_missing": 0,
        "recorder_error": 0,
        "recorder_invalid": 0,
        "streak_exhausted": 0,
        "retry_pending": 0,
        "isolated": 0,
    }
    primary_error_code: str | None = None
    force_terminated = False
    retry_pending_now = False
    runtime_samples: list[dict] = []
    max_task_elapsed_seconds = 0.0
    next_heartbeat = time.monotonic()
    discovery_closed = not continuous_discovery
    admission_was_closed = False
    empty_discovery_snapshots = 0
    next_discovery_at = time.monotonic()
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
    writer_executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="oa-global-writer",
    )

    def terminate_executor(
        target: object,
        *,
        owned_process_groups: bool = False,
    ) -> None:
        _terminate_executor(
            target,
            owned_process_groups=owned_process_groups,
        )

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
            prepare_elapsed, _action, reviewed, _ready_at, _depth, _blocked = (
                write_ready[index]
            )
            stopped_review(index, prepare_elapsed, reviewed)
        write_ready.clear()

    def request_pipeline_stop(error_code: str | None = None) -> None:
        nonlocal halted, primary_error_code
        if primary_error_code is None and error_code:
            primary_error_code = error_code
        halted = True
        runtime_control.request_cycle_stop()
        stop_reviews()
        stop_write_queue()

    def advance_write_head() -> None:
        nonlocal next_write_index
        if not strict_global_fifo:
            while next_write_index in skipped_write_indices:
                skipped_write_indices.remove(next_write_index)
                next_write_index += 1
        head = current_write_head()
        for queued_index, queued in list(write_ready.items()):
            if queued_index == head and queued[-1] is None:
                write_ready[queued_index] = (*queued[:-1], time.monotonic())
            elif queued_index != head and queued[-1] is not None:
                write_ready[queued_index] = (*queued[:-1], None)

    def current_write_head() -> int | None:
        if strict_global_fifo:
            return (
                min(
                    pending_write_indices,
                    key=lambda value: assigned_order_keys[value],
                )
                if pending_write_indices
                else None
            )
        return next_write_index

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
                    "rate_limit_events": (
                        codex_events["counts"].get("rate_limit_signal", 0)
                        if codex_events["status"] == "recorded"
                        else None
                    ),
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
            request_pipeline_stop(str(decision["reason"]))

    def apply_immediate_host_pressure(host: dict) -> None:
        nonlocal adaptive_healthy_windows
        nonlocal adaptive_pressure_active
        nonlocal adaptive_window_target
        nonlocal current_review_capacity
        if not adaptive or (adaptive_pressure_active and current_review_capacity == concurrency_control.CAPACITY_STEPS[0]):
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
            "allowed_workflows": sorted(allowed_workflows),
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
        if committed_order_key is not None:
            selection["committed_received_at"] = (
                committed_order_key[0].isoformat()
            )
            selection["committed_request_id"] = committed_order_key[1]
        if late_visible_keys:
            selection["known_late_visible_keys"] = sorted(late_visible_keys)
        if admission_deadline is not None:
            selection["admission_cutoff"] = admission_deadline.isoformat()
        return selection

    try:
        while (
            futures
            or preparation is not None
            or writer is not None
            or write_ready
            or (pending_reviews and not halted)
            or (
                (
                    next_index < len(outcomes)
                    or (continuous_discovery and not discovery_closed)
                )
                and not halted
            )
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
                    write_wait,
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
                writer_finished_at = time.monotonic()
                writer_busy_intervals.append(
                    (writer_started_at, writer_finished_at)
                )
                value = {
                    **completed,
                    "prepare_elapsed_seconds": prepare_elapsed,
                    "review_done_to_write_start_seconds": round(
                        max(0.0, writer_started_at - review_ready_at),
                        6,
                    ),
                    "writer_seconds": round(
                        max(0.0, writer_finished_at - writer_started_at),
                        6,
                    ),
                    "write_queue_depth_at_enqueue": enqueue_depth,
                    "write_wait_attribution": write_wait,
                }
                if (
                    value.get("status") == "retry_required"
                    and value.get("error_code") == "codex_decision_invalid"
                    and index not in decision_retries
                    and not halted
                ):
                    decision_retries.add(index)
                    review_histories[index] = [submitted_review]
                    pending_reviews[index] = (prepare_elapsed, action_file, False)
                else:
                    if (
                        value.get("status") == "retry_required"
                        and value.get("error_code") == "codex_decision_invalid"
                        and index in decision_retries
                        and not halted
                    ):
                        try:
                            quarantine = quarantine_review(
                                action_file,
                                "codex_decision_invalid",
                            )
                        except Exception:
                            quarantine = {}
                        if all(
                            quarantine.get(field) == expected
                            for field, expected in {
                                "action": "needs_admin",
                                "execution_state": "needs_admin",
                                "business_state": "not_available",
                                "write_state": "not_requested",
                                "reason": "codex_decision_invalid",
                            }.items()
                        ):
                            value = {
                                **value,
                                "status": "quarantined",
                                "execution_state": "needs_admin",
                                "business_state": "not_available",
                                "write_state": "not_requested",
                                "error_code": "codex_decision_invalid",
                            }
                        else:
                            value = {
                                **value,
                                "status": "failed",
                                "error_code": (
                                    "codex_decision_quarantine_failed"
                                ),
                            }
                    if value.get("status") == "retry_required":
                        value = {**value, "status": "failed"}
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="completed",
                        value=value,
                    )
                    review_histories.pop(index, None)
                    if value.get("status") not in {
                        "completed",
                        "quarantined",
                    }:
                        request_pipeline_stop(value.get("error_code"))
                    else:
                        if strict_global_fifo:
                            pending_write_indices.remove(index)
                        else:
                            next_write_index += 1
                        advance_write_head()
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
                request_pipeline_stop(
                    "production_probe_prepare_timeout"
                    if prepare_timed_out
                    else "worker_task_timeout"
                )
                continue

            if now >= next_heartbeat:
                try:
                    host = runtime_status()
                except Exception:
                    host = {"status": "unknown"}
                resource_paused = host.get("status") not in {"healthy", "pressure", "critical"}
                runtime_samples.append(
                    {
                        "elapsed_seconds": round(now - started_at, 3),
                        "pending_tasks": max(0, count - next_index),
                        "active_workers": len(futures)
                        + int(preparation is not None),
                        "active_reviews": len(futures),
                        "review_capacity": current_review_capacity,
                        "resource_paused": resource_paused,
                        "pending_reviews": len(pending_reviews),
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
                    request_pipeline_stop("host_critical")

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
                except Exception as error:
                    prepared = {
                        "action": "failed",
                        "error_code": "production_probe_prepare_failed",
                        "prepare_failure": prepare_failure.describe(
                            "child_exception",
                            error=error,
                            stop_action="cycle_stop",
                        ),
                    }
                preparation = None
                preparation_stop_deadline = None
                raw_late_keys = prepared.get("late_visible_keys", [])
                raw_late_pending_count = prepared.get(
                    "late_visible_pending_count",
                    0,
                )
                if (
                    not isinstance(raw_late_keys, list)
                    or not isinstance(raw_late_pending_count, int)
                    or isinstance(raw_late_pending_count, bool)
                    or raw_late_pending_count < 0
                    or any(
                        not isinstance(value, str)
                        or len(value) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in value
                        )
                        for value in raw_late_keys
                    )
                ):
                    prepared = {
                        "action": "failed",
                        "error_code": "production_probe_prepare_failed",
                        "prepare_failure": prepare_failure.describe(
                            "late_visible_contract",
                            stop_action="cycle_stop",
                        ),
                    }
                else:
                    late_visible_keys.update(raw_late_keys)
                    late_visible_pending_count = raw_late_pending_count
                if prepared.get("action") == "late_visible":
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="completed",
                        value={
                            "status": "late_visible",
                            "business_state": "not_available",
                            "write_state": "not_requested",
                            "prepare_elapsed_seconds": prepare_elapsed,
                            "process_attempts": 0,
                            "process_retries": 0,
                            "token_usage": {"status": "unknown"},
                        },
                    )
                    skipped_write_indices.add(index)
                    empty_discovery_snapshots = 0
                    advance_write_head()
                elif prepared.get("action") in {"no_action", "admission_closed"}:
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="completed",
                        value={
                            "status": "no_action",
                            "business_state": "not_available",
                            "write_state": "not_requested",
                            "prepare_elapsed_seconds": prepare_elapsed,
                            "process_attempts": 0,
                            "process_retries": 0,
                            "token_usage": {"status": "unknown"},
                        },
                    )
                    skipped_write_indices.add(index)
                    advance_write_head()
                    if prepared.get("action") == "admission_closed":
                        discovery_closed = True
                        admission_was_closed = True
                    elif continuous_discovery:
                        if futures or write_ready or writer is not None:
                            empty_discovery_snapshots = 0
                        else:
                            empty_discovery_snapshots += 1
                            if empty_discovery_snapshots >= 2:
                                discovery_closed = True
                        next_discovery_at = time.monotonic() + 0.05
                elif (
                    prepared.get("action") == "quarantined"
                    and prepared.get("write_state") == "not_requested"
                    and prepared.get("error_code") in ITEM_LOCAL_QUARANTINE_REASONS
                ):
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="completed",
                        value={
                            "status": "quarantined",
                            "business_state": "not_available",
                            "write_state": "not_requested",
                            "error_code": str(
                                prepared["error_code"]
                            ),
                            "prepare_elapsed_seconds": prepare_elapsed,
                            "process_attempts": 0,
                            "process_retries": 0,
                            "token_usage": {"status": "unknown"},
                        },
                    )
                    skipped_write_indices.add(index)
                    advance_write_head()
                elif prepared.get("action") != "review_required":
                    _record_prepare_failure(
                        prepared.get("prepare_failure"),
                        run_id=run_id,
                        controller_instance_id=controller_instance_id,
                    )
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
                    request_pipeline_stop(
                        str(
                            prepared.get("error_code")
                            or "production_probe_prepare_failed"
                        )
                    )
                    if not prepare_executor_terminated:
                        terminate_executor(
                            prepare_executor,
                            owned_process_groups=True,
                        )
                        prepare_executor_terminated = True
                else:
                    empty_discovery_snapshots = 0
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
                                request_pipeline_stop(
                                    "production_probe_duplicate_action"
                                )
                    elif halted or runtime_control.stop_requested():
                        stopped_review(index, prepare_elapsed, None)
                        halted = True
                    else:
                        unblocked_duplicate_retries = 0
                        if strict_global_fifo:
                            try:
                                identity = validated_assigned_identity(action_file)
                                assigned_identities[action_file] = identity
                                received_at = _received_at_utc(identity[1])
                                order_key = (received_at, identity[0])
                                if (
                                    committed_order_key is not None
                                    and order_key < committed_order_key
                                ):
                                    raise ValueError(
                                        "production_probe_late_claim"
                                    )
                                assigned_order_keys[index] = order_key
                                pending_write_indices.add(index)
                            except ValueError as error:
                                _record_prepare_failure(
                                    prepare_failure.describe(
                                        "identity_validation",
                                        reason_code=(
                                            str(error)
                                            if prepare_failure.is_code(str(error))
                                            else "production_probe_prepare_failed"
                                        ),
                                        stop_action="cycle_stop",
                                        action_file=str(action_file),
                                        **_prepare_failure_item(action_file),
                                    ),
                                    action_file=action_file,
                                    run_id=run_id,
                                    controller_instance_id=(
                                        controller_instance_id
                                    ),
                                )
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
                                request_pipeline_stop(
                                    "production_probe_prepare_failed"
                                )
                                continue
                        assigned.add(action_file)
                        advance_write_head()
                        pending_reviews[index] = (prepare_elapsed, action_file, True)

            done = {future for future in futures if future.done()}
            reviewed_now: list[tuple[int, float, Path, dict]] = []
            failed_now = False
            first_review_error_code: str | None = None
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
                    if first_review_error_code is None:
                        first_review_error_code = "worker_process_failed"
                    continue
                if value.get("status") != "reviewed":
                    error_code = str(
                        value.get("error_code") or "worker_process_failed"
                    )
                    if (
                        error_code in ITEM_LOCAL_REVIEW_FAILURE_REASONS
                        or error_code == "codex_process_timeout"
                    ):
                        consecutive_review_failures += 1
                        if _review_failure_streak_reached(
                            consecutive_review_failures
                        ):
                            request_pipeline_stop(
                                REVIEW_FAILURE_CIRCUIT_BREAKER_ERROR
                            )
                    else:
                        consecutive_review_failures = 0
                    reported_value = (
                        _merge_review_attempts(
                            [*review_histories[index], value]
                        )
                        if index in review_histories
                        else value
                    )
                    bounded_review_failure = (
                        codex_review_worker.is_bounded_invalid_decision(value)
                        if error_code == "codex_decision_invalid"
                        else codex_review_worker.is_bounded_prewrite_failure(value)
                    )
                    if (
                        error_code in ITEM_LOCAL_REVIEW_FAILURE_REASONS
                        or error_code == "codex_process_timeout"
                    ):
                        review_retry_diagnostics["item_local_failure"] += 1
                        review_retry_diagnostics[
                            "bounded" if bounded_review_failure else "unbounded"
                        ] += 1
                    if (
                        (error_code in ITEM_LOCAL_REVIEW_FAILURE_REASONS
                         or error_code == "codex_process_timeout")
                        and bounded_review_failure
                    ):
                        failure_round = None
                        production_failure_round_recorder = (
                            record_failure_round
                            or (
                                codex_review_worker.record_production_failure_round
                                if strict_global_fifo and continuous_discovery
                                else None
                            )
                        )
                        if production_failure_round_recorder is None:
                            review_retry_diagnostics["recorder_missing"] += 1
                        else:
                            try:
                                failure_round = production_failure_round_recorder(
                                    action_file,
                                    error_code,
                                ).get("production_failure_round")
                            except Exception:
                                failure_round = None
                                review_retry_diagnostics["recorder_error"] += 1
                            if not isinstance(failure_round, int):
                                if failure_round is not None:
                                    review_retry_diagnostics[
                                        "recorder_invalid"
                                    ] += 1
                        if isinstance(failure_round, int) and failure_round < 3:
                            review_retry_diagnostics["retry_pending"] += 1
                            retry_pending_now = True
                            retry_value = {
                                **reported_value,
                                "status": "retry_pending",
                                "execution_state": "review_required",
                                "business_state": "not_available",
                                "write_state": "authorized",
                                "error_code": "codex_review_retry_pending",
                                "production_failure_round": failure_round,
                                "prepare_elapsed_seconds": prepare_elapsed,
                            }
                            outcomes[index] = process_pool.TaskOutcome(
                                index=index,
                                status="completed",
                                value=retry_value,
                            )
                            if strict_global_fifo:
                                pending_write_indices.remove(index)
                            else:
                                skipped_write_indices.add(index)
                            review_histories.pop(index, None)
                            advance_write_head()
                            continue
                        if isinstance(failure_round, int):
                            review_retry_diagnostics["streak_exhausted"] += 1
                        try:
                            quarantine = quarantine_review(
                                action_file,
                                error_code,
                            )
                        except Exception:
                            quarantine = {}
                        review_retry_diagnostics["isolated"] += 1
                        expected_reason = (
                            "codex_decision_invalid"
                            if error_code == "codex_decision_invalid"
                            else "codex_review_failed"
                        )
                        if all(
                            quarantine.get(field) == expected
                            for field, expected in {
                                "action": "needs_admin",
                                "execution_state": "needs_admin",
                                "business_state": "not_available",
                                "write_state": "not_requested",
                                "reason": expected_reason,
                            }.items()
                        ):
                            quarantined_value = {
                                **reported_value,
                                "status": "quarantined",
                                "execution_state": "needs_admin",
                                "business_state": "not_available",
                                "write_state": "not_requested",
                                "error_code": error_code,
                                "prepare_elapsed_seconds": prepare_elapsed,
                            }
                            outcomes[index] = process_pool.TaskOutcome(
                                index=index,
                                status="completed",
                                value={**quarantined_value, "status": "failed"}
                                if error_code == "codex_process_timeout" else quarantined_value,
                            )
                            if error_code == "codex_process_timeout":
                                failed_now = True
                                first_review_error_code = first_review_error_code or error_code
                                continue
                            if strict_global_fifo:
                                pending_write_indices.remove(index)
                            else:
                                skipped_write_indices.add(index)
                            review_histories.pop(index, None)
                            advance_write_head()
                            observe_adaptive_window(quarantined_value)
                            continue
                        value = {
                            **reported_value,
                            "status": "failed",
                            "error_code": "codex_review_quarantine_failed",
                        }
                    else:
                        value = reported_value
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="completed",
                        value={
                            **value,
                            "prepare_elapsed_seconds": prepare_elapsed,
                        },
                    )
                    review_histories.pop(index, None)
                    failed_now = True
                    if first_review_error_code is None:
                        first_review_error_code = str(
                            value.get("error_code") or "worker_process_failed"
                        )
                    continue
                reviewed_now.append(
                    (index, prepare_elapsed, action_file, value)
                )
                consecutive_review_failures = 0
            if failed_now:
                for index, prepare_elapsed, _action, reviewed in reviewed_now:
                    stopped_review(index, prepare_elapsed, reviewed)
                request_pipeline_stop(first_review_error_code)
            elif halted:
                for index, prepare_elapsed, _action, reviewed in reviewed_now:
                    stopped_review(index, prepare_elapsed, reviewed)
            else:
                for index, prepare_elapsed, action_file, reviewed in reviewed_now:
                    merged = _merge_review_attempts(
                        [*review_histories.get(index, []), reviewed]
                    )
                    head = current_write_head()
                    write_ready[index] = (
                        prepare_elapsed,
                        action_file,
                        merged,
                        (ready_at := time.monotonic()),
                        len(write_ready) + 1,
                        ready_at if index == head else None,
                    )
                    max_write_queue_depth = max(
                        max_write_queue_depth,
                        len(write_ready),
                    )
                advance_write_head()

            while pending_reviews and not halted and not resource_paused and len(futures) < current_review_capacity:
                index = min(pending_reviews)
                prepare_elapsed, action_file, first_attempt = pending_reviews.pop(index)
                submit_review(index, prepare_elapsed, action_file, first_attempt=first_attempt)

            write_head = current_write_head()
            if (
                not halted
                and not resource_paused
                and writer is None
                and (preparation is None or not strict_global_fifo)
                and write_head is not None
                and write_head in write_ready
                and not runtime_control.stop_requested()
            ):
                (
                    prepare_elapsed,
                    action_file,
                    reviewed,
                    review_ready_at,
                    enqueue_depth,
                    became_write_head_at,
                ) = write_ready.pop(write_head)
                index = write_head
                writer_started_at = time.monotonic()
                write_wait = _write_wait_attribution(
                    ready_at=review_ready_at,
                    write_started_at=writer_started_at,
                    writer_busy_intervals=writer_busy_intervals,
                    became_write_head_at=became_write_head_at,
                )
                if not _persist_write_wait_evidence(
                    action_file,
                    ready_at=review_ready_at,
                    write_started_at=writer_started_at,
                    queue_depth=enqueue_depth,
                    attribution=write_wait,
                    event_suffix=(2 if index in decision_retries else 1),
                ):
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="completed",
                        value={
                            **reviewed,
                            "status": "failed",
                            "business_state": "not_available",
                            "write_state": "not_requested",
                            "error_code": "observability_write_failed",
                            "prepare_elapsed_seconds": prepare_elapsed,
                        },
                    )
                    request_pipeline_stop("observability_write_failed")
                    continue
                if strict_global_fifo:
                    order_key = assigned_order_keys[index]
                    if (
                        committed_order_key is not None
                        and order_key < committed_order_key
                    ):
                        outcomes[index] = process_pool.TaskOutcome(
                            index=index,
                            status="completed",
                            value={
                                **reviewed,
                                "status": "failed",
                                "business_state": "not_available",
                                "write_state": "not_requested",
                                "error_code": "production_probe_fifo_mismatch",
                                "prepare_elapsed_seconds": prepare_elapsed,
                            },
                        )
                        request_pipeline_stop("production_probe_fifo_mismatch")
                        continue
                    committed_order_key = order_key
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
                    write_wait,
                )

            while (
                not halted
                and not resource_paused
                and preparation is None
                and (
                    next_index < len(outcomes)
                    or (continuous_discovery and not discovery_closed)
                )
                and time.monotonic() >= next_discovery_at
                and len(futures) + len(pending_reviews) < current_review_capacity
                and (
                    len(futures)
                    + len(pending_reviews)
                    + len(write_ready)
                    + int(preparation is not None)
                    < write_queue_capacity
                )
                and not preparation_deferred
            ):
                if next_index >= len(outcomes):
                    outcomes.append(None)
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
                        request_pipeline_stop("production_probe_prepare_failed")
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
                    request_pipeline_stop("production_probe_renew_failed")
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
                and (not pending_reviews or halted)
                and (
                    halted
                    or (
                        next_index >= len(outcomes)
                        and (not continuous_discovery or discovery_closed)
                    )
                )
            ):
                break
            waitables = [*futures]
            if preparation is not None:
                waitables.append(preparation[0])
            if writer is not None:
                waitables.append(writer[0])
            if resource_paused and not halted:
                time.sleep(min(0.25, heartbeat_interval_sec))
            elif waitables:
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
    processes_reaped = _processes_reaped(
        known_processes,
        timeout_seconds=5,
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
        outcome
        for outcome in outcomes
        if outcome is not None
        and not (
            isinstance(outcome.value, dict)
            and outcome.value.get("status") in {"late_visible", "no_action"}
        )
    )
    if (
        halted
        and not retry_pending_now
        and not force_terminated
        and not runtime_control.stop_requested()
    ):
        runtime_control.request_cycle_stop()
    # 固定原因计数:只记分类计数,便于下一轮定位"为什么失败单被隔离"。
    try:
        json_artifact.write_json_atomic(
            Path(root) / "runtime" / "control" / "review-retry-diagnostics.json",
            {
                "schema_version": 1,
                "kind": "preaudit_review_retry_diagnostics",
                "counts": dict(sorted(review_retry_diagnostics.items())),
            },
            indent=2,
        )
    except (OSError, TypeError, ValueError):
        pass
    return process_pool.PoolResult(
        requested=len(completed_outcomes),
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
        primary_error_code=primary_error_code,
        recommended_capacity=current_review_capacity,
        capacity_decisions=tuple(capacity_decisions),
        discovery_refreshes=(next_index if strict_global_fifo else 0),
        late_visible_count=len(late_visible_keys - initial_late_visible_keys),
        late_visible_keys=tuple(sorted(late_visible_keys)),
        committed_received_at=(
            committed_order_key[0].isoformat()
            if committed_order_key is not None
            else None
        ),
        committed_request_id=(
            committed_order_key[1] if committed_order_key is not None else None
        ),
        late_visible_pending_count=late_visible_pending_count,
        discovery_exhausted=(
            continuous_discovery
            and discovery_closed
            and not admission_was_closed
            and not halted
        ),
        admission_closed=admission_was_closed,
    )


def _run_global_dispatched_tasks(
    count: int,
    capacity: int,
    *,
    adaptive: bool = False,
    max_capacity: int | None = None,
    historical_unknown_identities: frozenset[tuple[str, str]] | None = None,
    recovery_action_files: Sequence[Path] = (),
    committed_received_at: str | None = None,
    committed_request_id: str | None = None,
    known_late_visible_keys: frozenset[str] = frozenset(),
    admission_cutoff: str | None = None,
    allowed_workflows: frozenset[str] = ROLLING_WORKFLOW_KEYS,
    run_id: str | None = None,
    controller_instance_id: str | None = None,
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
        "committed_received_at": committed_received_at,
        "committed_request_id": committed_request_id,
        "known_late_visible_keys": known_late_visible_keys,
        "admission_cutoff": admission_cutoff,
        "continuous_discovery": True,
        "allowed_workflows": allowed_workflows,
        "run_id": run_id,
        "controller_instance_id": controller_instance_id,
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
            runtime_control.request_cycle_stop()
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
        runtime_control.request_cycle_stop()
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


def _production_reconciliation(
    workflow_keys: frozenset[str] = ROLLING_WORKFLOW_KEYS,
) -> dict:
    root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    return cli._live_reconciliation(
        root,
        workflow_keys=workflow_keys,
    )


def _supported_pending(
    live: dict,
    workflow_keys: frozenset[str] = ROLLING_WORKFLOW_KEYS,
) -> int:
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
    for workflow_key in workflow_keys:
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
    workflow_keys: frozenset[str] = ROLLING_WORKFLOW_KEYS,
    *, recovery_action_files: Sequence[Path] = (),
) -> tuple[int, int, int, int]:
    live = live_status()
    pending = _supported_pending(live, workflow_keys)
    oa = live.get("oa")
    unauthorized = oa.get("unsupported", 0) if isinstance(oa, dict) else 0
    if (
        not isinstance(unauthorized, int)
        or isinstance(unauthorized, bool)
        or unauthorized < 0
    ):
        raise ValueError("production_drain_live_status_invalid")
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
        or not _continuation_is_ready(reconciled, recovery_action_files=recovery_action_files)
    ):
        raise ValueError("production_rolling_reconciliation_invalid")
    return pending, quarantined, actionable, unauthorized


def _quarantined_pending_count() -> int:
    return int(_production_reconciliation()["quarantine"]["pending_quarantined"])


def _local_quarantine_count() -> int:
    return reconciliation._quarantine_summary(
        PROJECT_ROOT / "runtime" / "finance_preaudit_flows",
        _latest_local_states(),
        [],
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
            runtime_control.request_cycle_stop()
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
            runtime_control.request_cycle_stop()
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
        return {
            "samples": 0,
            "p50": "unknown",
            "p95": "unknown",
            "p99": "unknown",
        }
    ordered = sorted(max(0.0, float(value)) for value in values)

    def nearest(percent: int) -> float:
        rank = max(1, (len(ordered) * percent + 99) // 100)
        return round(ordered[rank - 1], 6)

    return {
        "samples": len(ordered),
        "p50": nearest(50) if len(ordered) >= 5 else "unknown",
        "p95": nearest(95) if len(ordered) >= 20 else "unknown",
        "p99": nearest(99) if len(ordered) >= 100 else "unknown",
    }


def _write_wait_attribution(
    *,
    ready_at: float,
    write_started_at: float,
    writer_busy_intervals: Sequence[tuple[float, float]],
    became_write_head_at: float | None,
) -> dict:
    total = max(0.0, write_started_at - ready_at)
    if (
        became_write_head_at is None
        or became_write_head_at < ready_at
        or became_write_head_at > write_started_at
    ):
        return {"status": "unknown"}

    def overlap(start: float, end: float) -> float:
        return sum(
            max(0.0, min(end, interval_end) - max(start, interval_start))
            for interval_start, interval_end in writer_busy_intervals
        )

    writer_busy = overlap(ready_at, write_started_at)
    writer_busy = min(total, writer_busy)
    before_head = max(0.0, became_write_head_at - ready_at)
    fifo_head = max(0.0, before_head - overlap(ready_at, became_write_head_at))
    rounded_total = round(total, 6)
    rounded_busy = min(rounded_total, round(writer_busy, 6))
    rounded_fifo = min(
        max(0.0, rounded_total - rounded_busy),
        round(fifo_head, 6),
    )
    rounded_scheduling = max(
        0.0,
        round(rounded_total - rounded_busy - rounded_fifo, 6),
    )
    return {
        "status": "exact",
        "total_seconds": rounded_total,
        "writer_busy_seconds": rounded_busy,
        "fifo_head_of_line_seconds": rounded_fifo,
        "controller_scheduling_seconds": rounded_scheduling,
    }


def _rolling_wave_metrics(result: process_pool.PoolResult[dict]) -> dict:
    business_results: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    usage_workers: Counter[str] = Counter()
    token_totals: Counter[str] = Counter()
    completed = 0
    failed = 0
    isolated_quarantines = 0
    retry_pending = 0
    process_attempts = 0
    process_retries = 0
    prepare_active_seconds = 0.0
    worker_values: list[dict] = []
    review_to_write: list[float] = []
    writer_seconds: list[float] = []
    write_wait_samples: list[dict] = []
    queue_depths: list[int] = []
    primary_error_code = result.primary_error_code
    for outcome in result.outcomes:
        value = outcome.value if isinstance(outcome.value, dict) else {}
        worker_values.append(value)
        worker_status = value.get("status")
        if outcome.status == "completed" and worker_status in {
            "completed",
            "quarantined",
            "retry_pending",
        }:
            if worker_status == "retry_pending":
                completed += 1
                retry_pending += 1
                error_code = value.get("error_code") or outcome.error_code
                if isinstance(error_code, str):
                    error_counts[error_code] += 1
                process_attempts += int(value.get("process_attempts") or 0)
                process_retries += int(value.get("process_retries") or 0)
                continue
            if worker_status == "quarantined" and not (
                value.get("write_state") == "not_requested"
                and value.get("error_code") in ITEM_LOCAL_QUARANTINE_REASONS
            ):
                failed += 1
            else:
                completed += 1
                if worker_status == "quarantined":
                    isolated_quarantines += 1
                business_state = value.get("business_state")
                if isinstance(business_state, str):
                    business_results[business_state] += 1
        elif outcome.status not in {"timed_out", "stopped"}:
            failed += 1
        error_code = value.get("error_code") or outcome.error_code
        if isinstance(error_code, str):
            error_counts[error_code] += 1
            if (
                primary_error_code is None
                and error_code
                not in {"stop_requested", "worker_cancelled_after_timeout"}
            ):
                primary_error_code = error_code
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
        write_wait = value.get("write_wait_attribution")
        if (
            isinstance(write_wait, dict)
            and write_wait.get("status") == "exact"
            and all(
                isinstance(write_wait.get(field), (int, float))
                and not isinstance(write_wait.get(field), bool)
                for field in (
                    "total_seconds",
                    "writer_busy_seconds",
                    "fifo_head_of_line_seconds",
                    "controller_scheduling_seconds",
                )
            )
        ):
            write_wait_samples.append(write_wait)
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
    rate_limit_events = codex_events["counts"].get("rate_limit_signal")
    if rate_limit_events is None and codex_events["status"] == "recorded":
        rate_limit_events = 0
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
        "write_wait_attribution": {
            "samples": len(write_wait_samples),
            "writer_busy_seconds": round(
                sum(item["writer_busy_seconds"] for item in write_wait_samples),
                6,
            ),
            "fifo_head_of_line_seconds": round(
                sum(
                    item["fifo_head_of_line_seconds"]
                    for item in write_wait_samples
                ),
                6,
            ),
            "controller_scheduling_seconds": round(
                sum(
                    item["controller_scheduling_seconds"]
                    for item in write_wait_samples
                ),
                6,
            ),
        },
    }
    return {
        "requested": result.requested,
        "started": result.started,
        "completed": completed,
        "isolated_quarantines": isolated_quarantines,
        "retry_pending": retry_pending,
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
        "primary_error_code": primary_error_code,
        "codex_events": codex_events,
        "transient_error_events": transient_error_events,
        "rate_limit_events": rate_limit_events,
        "pipeline": pipeline,
        "processes_reaped": result.processes_reaped,
        "discovery_refreshes": result.discovery_refreshes,
        "late_visible": result.late_visible_count,
        "late_visible_pending": result.late_visible_pending_count,
        "discovery_exhausted": result.discovery_exhausted,
        "admission_closed": result.admission_closed,
        "recommended_capacity": result.recommended_capacity,
        "capacity_decisions": list(result.capacity_decisions),
        "_pipeline_review_wait_samples": review_to_write,
        "_pipeline_write_wait_samples": write_wait_samples,
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
    allowed_workflows: frozenset[str] = ROLLING_WORKFLOW_KEYS,
    confirm_stale_children_reaped: bool = False,
) -> dict:
    recovery_queue = tuple(
        Path(path).expanduser().resolve() for path in recovery_action_files
    )
    if (
        not allowed_workflows
        or not allowed_workflows.issubset(ROLLING_WORKFLOW_KEYS)
    ):
        raise ValueError("production_rolling_workflow_scope_invalid")
    if recovery_queue and pipeline_runner is not _run_global_dispatched_tasks:
        raise ValueError("production_rolling_recovery_runner_invalid")
    scoped_reconcile_status = (
        (lambda: _production_reconciliation(allowed_workflows))
        if reconcile_status is _production_reconciliation
        else reconcile_status
    )
    recovered_cycle_stop = _recover_stale_cycle_stop(
        live_status=live_status,
        reconcile_status=scoped_reconcile_status,
        allowed_workflows=allowed_workflows,
        confirm_stale_children_reaped=confirm_stale_children_reaped,
        recovery_action_files=recovery_queue,
    )
    controller_handoff.assert_ready_for_start()
    try:
        with runtime_control.global_controller_lease():
            cache_maintenance = attachment_cache.prune_expired()
            admission_cutoff = (
                datetime.now(timezone.utc) + timedelta(minutes=60)
            ).isoformat()
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
                    "admission_cutoff": admission_cutoff,
                }
            )
            children_reaped = True
            pending_recoveries = recovery_queue
            committed_received_at: str | None = None
            committed_request_id: str | None = None
            known_late_visible_keys: frozenset[str] = frozenset()

            def tracked_pipeline_runner(
                count: int,
                review_capacity: int,
            ) -> process_pool.PoolResult[dict]:
                nonlocal children_reaped, pending_recoveries
                nonlocal committed_received_at, committed_request_id
                nonlocal known_late_visible_keys
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
                        committed_received_at=committed_received_at,
                        committed_request_id=committed_request_id,
                        known_late_visible_keys=known_late_visible_keys,
                        admission_cutoff=admission_cutoff,
                        allowed_workflows=allowed_workflows,
                        run_id=manifest["run_id"],
                        controller_instance_id=(
                            manifest["controller_instance_id"]
                        ),
                    )
                    pending_recoveries = ()
                    committed_received_at = wave_result.committed_received_at
                    committed_request_id = wave_result.committed_request_id
                    known_late_visible_keys = frozenset(
                        wave_result.late_visible_keys
                    )
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
                    reconcile_status=scoped_reconcile_status,
                    max_waves=max_waves,
                    recovery_count=len(recovery_queue),
                    recovery_action_files=recovery_queue,
                    allowed_workflows=allowed_workflows,
                )
                if result["processes_reaped"] is not True:
                    children_reaped = False
                    runtime_control.request_cycle_stop()
                    raise ControllerChildrenActiveError(
                        result.get("primary_error_code")
                    )
            except BaseException:
                if not children_reaped:
                    runtime_control.request_cycle_stop()
                controller_handoff.finish(
                    manifest,
                    status="failed",
                    children_reaped=children_reaped,
                    health="degraded",
                )
                _clear_finished_cycle_stop(children_reaped=children_reaped)
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
            _clear_finished_cycle_stop(children_reaped=True)
            return {
                **result,
                "recovered_cycle_stop": recovered_cycle_stop,
                "cache_maintenance": cache_maintenance,
                "controller_handoff": handoff,
            }
    except RuntimeError as error:
        if str(error) == "controller_already_active":
            raise ValueError("production_rolling_controller_active") from error
        raise


def _recover_stale_cycle_stop(
    *,
    live_status: LiveStatus,
    reconcile_status: Callable[[], dict],
    allowed_workflows: frozenset[str],
    confirm_stale_children_reaped: bool = False,
    recovery_action_files: Sequence[Path] = (),
) -> bool:
    if not runtime_control.stop_requested():
        if confirm_stale_children_reaped:
            raise ValueError("production_cycle_stop_recovery_not_required")
        return False
    if runtime_control.stop_reason() != "controller_cycle_stop":
        raise ValueError("production_probe_stopped")
    handoff = controller_handoff.status()
    if (
        confirm_stale_children_reaped
        and handoff.get("ready_for_start") is True
    ):
        raise ValueError("production_cycle_stop_recovery_not_required")
    if (
        handoff.get("controller_active") is not False
        or handoff.get("writer_active") is not False
        or handoff.get("active_request_writers") != 0
    ):
        raise ValueError("production_cycle_stop_recovery_blocked")
    try:
        live = live_status()
        reconciled = reconcile_status()
        _pending, _quarantined, _actionable, _unauthorized = _rolling_snapshot(
            lambda: live,
            lambda: reconciled,
            allowed_workflows,
            recovery_action_files=recovery_action_files,
        )
        oa = live.get("oa")
        if (
            not isinstance(oa, dict)
            or oa.get("execution_identity") != "verified"
            or not _continuation_is_ready(reconciled, recovery_action_files=recovery_action_files)
        ):
            raise ValueError("production_cycle_stop_recovery_blocked")
    except (RuntimeError, ValueError) as error:
        if str(error) == "production_cycle_stop_recovery_blocked":
            raise
        raise ValueError("production_cycle_stop_recovery_blocked") from error
    if handoff.get("ready_for_start") is not True:
        if confirm_stale_children_reaped is not True:
            raise ValueError("production_cycle_stop_recovery_blocked")
        try:
            controller_handoff.recover_confirmed_children_reaped(
                confirmed=True
            )
        except ValueError as error:
            raise ValueError("production_cycle_stop_recovery_blocked") from error
        handoff = controller_handoff.status()
        if handoff.get("ready_for_start") is not True:
            raise ValueError("production_cycle_stop_recovery_blocked")
    try:
        cleared = runtime_control.clear_cycle_stop()
    except RuntimeError as error:
        raise ValueError("production_cycle_stop_recovery_blocked") from error
    if not cleared:
        raise ValueError("production_cycle_stop_recovery_blocked")
    return True


def _continuation_is_ready(
    reconciled: dict, *, recovery_action_files: Sequence[Path] = (),
) -> bool:
    continuation = reconciled.get("continuation")
    quarantine = reconciled.get("quarantine")
    if (
        not isinstance(continuation, dict)
        or set(continuation) != {
            "status", "scope", "isolated_pending", "blocking_reasons"
        }
        or not isinstance(quarantine, dict)
    ):
        return False
    isolated = continuation.get("isolated_pending")
    ready = (
        continuation.get("status") == "ready"
        and isinstance(isolated, int)
        and not isinstance(isolated, bool)
        and isolated >= 0
        and isolated == quarantine.get("pending_quarantined")
        and continuation.get("scope") == ("item" if isolated else "none")
        and continuation.get("blocking_reasons") == {}
    )

    # Historical, isolated failures remain visible and stop admission until an
    # explicit recovery selection is supplied. The listener may keep checking
    # this gate, but it must not approve later items automatically.
    deferred_historical = (
        continuation.get("status") == "blocked"
        and continuation.get("blocking_reasons") == {}
        and isinstance(isolated, int)
        and not isinstance(isolated, bool)
        and isolated > 0
        and isolated == quarantine.get("pending_quarantined")
    )
    if deferred_historical:
        return bool(recovery_action_files)

    if ready:
        # Production may continue ordinary pending work while trusted item
        # quarantines remain visible; quarantined identities stay excluded.
        return True
    if not recovery_action_files:
        return False
    # This admits only explicitly selected preparation work; FlowEngine still
    # revalidates live identity and no-write evidence before creating a new attempt.
    reasons = continuation.get("blocking_reasons")
    if (continuation.get("status") != "blocked" or continuation.get("scope") != "controller"
            or not isinstance(reasons, dict) or set(reasons) != {"pending_failure_not_isolated"}
            or not isinstance(isolated, int) or isinstance(isolated, bool) or isolated < 0):
        return False
    root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    try:
        candidates = _recovery_candidates(recovery_action_files, assigned_identities=set())
        latest = {state["action_file"]: state for state in reconciliation.latest_states(root)}
        selected = set()
        for candidate in candidates:
            path = Path(candidate["recovery_action_file"])
            state = latest.get(str(path))
            action = _read_object(path)
            if not isinstance(state, dict) or any(action.get(key) != state.get(key) for key in (
                "action_file", "state_file", "workflow_key", "case_key", "attempt",
                "execution_state", "write_state", "reason",
            )) or action.get("action") != "needs_admin":
                return False
            if not flow_engine.FlowEngine.recoverable_no_write(root, path):
                return False
            selected.add((state["case_key"], state["workflow_key"], state["attempt"]))
        items = reconciled["manual_review"]["items"]
        recovered = sum(
            (item["item_key"], item["workflow_key"], item["attempt"]) in selected
            and item["category"] == "pending_not_handled"
            and item["write_state"] == "not_requested"
            and item["failure_code"] in RECOVERABLE_NO_WRITE_REASONS
            and item["scope"] != "item"
            for item in items
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return (type(reasons["pending_failure_not_isolated"]) is int
            and recovered == reasons["pending_failure_not_isolated"]
            and isolated + recovered == quarantine.get("pending_quarantined"))


def _clear_finished_cycle_stop(*, children_reaped: bool) -> None:
    if (
        not children_reaped
        or runtime_control.writer_active()
        or runtime_control.active_request_writers() != 0
    ):
        return
    try:
        runtime_control.clear_cycle_stop()
    except RuntimeError as error:
        raise ValueError("production_cycle_stop_cleanup_failed") from error


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
    recovery_action_files: Sequence[Path] = (),
    allowed_workflows: frozenset[str] = ROLLING_WORKFLOW_KEYS,
) -> dict:
    if (
        capacity not in concurrency_control.CAPACITY_STEPS
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
        (
            initial_pending,
            quarantined_pending,
            initial_actionable,
            unauthorized_pending,
        ) = (
            _rolling_snapshot(
                live_status,
                reconcile_status,
                allowed_workflows,
                recovery_action_files=recovery_action_files,
            )
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
    discovery_refreshes = 1
    late_visible_pending = 0
    post_boundary_actionable = 0
    admission_closed = False
    empty_snapshots = 0 if actionable else 1
    required_empty_snapshots = 2
    while actionable or empty_snapshots < required_empty_snapshots:
        if not actionable:
            quarantined_before_refresh = quarantined_pending
            try:
                (
                    pending,
                    quarantined_pending,
                    actionable,
                    unauthorized_pending,
                ) = _rolling_snapshot(
                    live_status,
                    reconcile_status,
                    allowed_workflows,
                )
            except ValueError:
                runtime_control.request_cycle_stop()
                raise
            discovery_refreshes += 1
            if quarantined_pending > quarantined_before_refresh:
                runtime_control.request_cycle_stop()
                status = "failed"
                stop_reason = "quarantine_increased"
                break
            if actionable:
                empty_snapshots = 0
                continue
            empty_snapshots += 1
            continue
        if len(waves) >= max_waves:
            runtime_control.request_cycle_stop()
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
        late_visible_pending = int(wave.get("late_visible_pending") or 0)
        if int(wave.get("retry_pending") or 0) > 0:
            # A transient first/second-round review failure is persisted on the
            # open attempt. End this production cycle quietly; the next cycle
            # will retry it and only round three can create an isolation.
            status = "completed"
            stop_reason = "review_retry_pending"
            break
        if (
            wave["failed"]
            or wave["timed_out"]
            or wave["stopped"]
            or wave["completed"] != wave["requested"]
        ):
            runtime_control.request_cycle_stop()
            status = "failed"
            if adaptive and int(wave.get("rate_limit_events") or 0) > 0:
                current_capacity = concurrency_control.CAPACITY_STEPS[0]
                last_decision = {
                    "action": "decrease",
                    "next_capacity": concurrency_control.CAPACITY_STEPS[0],
                    "healthy_windows": 0,
                    "reason": "rate_limit_pressure",
                }
                stop_reason = "rate_limit_pressure"
                break
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
        quarantined_before_wave = quarantined_pending
        try:
            (
                pending,
                quarantined_pending,
                actionable,
                unauthorized_pending,
            ) = _rolling_snapshot(
                live_status,
                reconcile_status,
                allowed_workflows,
            )
        except ValueError:
            runtime_control.request_cycle_stop()
            raise
        discovery_refreshes += 1
        quarantine_growth = max(
            0,
            quarantined_pending - quarantined_before_wave,
        )
        if quarantine_growth > int(wave.get("isolated_quarantines") or 0):
            runtime_control.request_cycle_stop()
            status = "failed"
            stop_reason = "quarantine_increased"
            break
        if wave.get("admission_closed") is True:
            admission_closed = True
            unauthorized_pending += actionable
            actionable = 0
            empty_snapshots = required_empty_snapshots
            break
        if wave.get("discovery_exhausted") is True:
            unauthorized_pending += min(actionable, late_visible_pending)
            post_boundary_actionable = max(
                0,
                actionable - late_visible_pending,
            )
            actionable = 0
            empty_snapshots = required_empty_snapshots
            break
        empty_snapshots = 0 if actionable else 1
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
                        "rate_limit_events": wave.get("rate_limit_events"),
                        "codex_event_status": wave["codex_events"]["status"],
                    },
                    host=wave["runtime_health"],
                )
            healthy_windows = int(last_decision["healthy_windows"])
            if last_decision["action"] == "stop":
                runtime_control.request_cycle_stop()
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
    primary_error_code: str | None = None
    for wave in waves:
        business_results.update(wave["business_results"])
        error_counts.update(wave["error_counts"])
        if primary_error_code is None and isinstance(
            wave.get("primary_error_code"),
            str,
        ):
            primary_error_code = wave["primary_error_code"]
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
    rate_limit_events = codex_events["counts"].get("rate_limit_signal")
    if rate_limit_events is None and codex_events["status"] == "recorded":
        rate_limit_events = 0
    pipeline_wait_samples = [
        float(value)
        for wave in waves
        for value in wave.get("_pipeline_review_wait_samples", [])
    ]
    pipeline_write_wait_samples = [
        value
        for wave in waves
        for value in wave.get("_pipeline_write_wait_samples", [])
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
        "write_wait_attribution": {
            "samples": len(pipeline_write_wait_samples),
            "writer_busy_seconds": round(
                sum(
                    float(item["writer_busy_seconds"])
                    for item in pipeline_write_wait_samples
                ),
                6,
            ),
            "fifo_head_of_line_seconds": round(
                sum(
                    float(item["fifo_head_of_line_seconds"])
                    for item in pipeline_write_wait_samples
                ),
                6,
            ),
            "controller_scheduling_seconds": round(
                sum(
                    float(item["controller_scheduling_seconds"])
                    for item in pipeline_write_wait_samples
                ),
                6,
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
        "unauthorized_pending": unauthorized_pending,
        "empty_snapshots": empty_snapshots,
        "discovery_refreshes": discovery_refreshes,
        "prepare_discovery_refreshes": sum(
            int(wave.get("discovery_refreshes") or 0) for wave in waves
        ),
        "late_visible": sum(
            int(wave.get("late_visible") or 0) for wave in waves
        ),
        "admission_closed": admission_closed,
        "post_boundary_actionable": post_boundary_actionable,
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
        "primary_error_code": primary_error_code,
        "codex_events": codex_events,
        "transient_error_events": transient_error_events,
        "rate_limit_events": rate_limit_events,
        "pipeline": pipeline,
        "processes_reaped": processes_reaped,
        "token_usage": {
            "status": (
                "recorded"
                if usage_workers["recorded_workers"] > 0
                and not usage_workers["unknown_workers"]
                and not usage_workers["partial_workers"]
                else "partial"
                if usage_workers["recorded_workers"] or usage_workers["partial_workers"]
                else "unknown"
            ),
            **usage_workers,
            **token_totals,
        },
    }


def _verified_active_controller() -> dict | None:
    try:
        if runtime_control.stop_requested():
            return None
        handoff = controller_handoff.status()
    except Exception:
        # An unreadable status must retain the original failure and alert.
        return None
    if (
        handoff.get("status") == "active"
        and handoff.get("controller_active") is True
        and handoff.get("manifest_state") == "running"
        and handoff.get("lock_owner_consistent") is True
        and handoff.get("restart_verified") is True
        and handoff.get("code_matches_current") is True
    ):
        return handoff
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finance-preaudit-concurrency-probe")
    parser.add_argument("--capacity", type=int, default=2)
    parser.add_argument("--confirm-write", choices=("YES",))
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument(
        "--adaptive",
        action="store_true",
        default=True,
        help="按健康窗口逐级扩容（生产默认开启）",
    )
    parser.add_argument(
        "--fixed-capacity",
        dest="adaptive",
        action="store_false",
        help="关闭生产弹性扩容，固定使用 --capacity",
    )
    parser.add_argument("--waves", type=int, default=6)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--rolling", action="store_true")
    parser.add_argument(
        "--workflow",
        action="append",
        choices=sorted(ROLLING_WORKFLOW_KEYS),
        default=[],
    )
    parser.add_argument(
        "--recover-action-file",
        action="append",
        type=Path,
        default=[],
    )
    parser.add_argument(
        "--confirm-stale-children-reaped",
        choices=("YES",),
    )
    args = parser.parse_args(argv)
    controller_started = False
    try:
        if args.read_only:
            if (
                args.drain
                or args.rolling
                or args.recover_action_file
                or args.workflow
                or args.confirm_stale_children_reaped
            ):
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
                "allowed_workflows": (
                    frozenset(args.workflow)
                    if args.workflow
                    else ROLLING_WORKFLOW_KEYS
                ),
            }
            if args.recover_action_file:
                rolling_args["recovery_action_files"] = tuple(
                    args.recover_action_file
                )
            if args.confirm_stale_children_reaped == "YES":
                rolling_args["confirm_stale_children_reaped"] = True
            controller_started = True
            payload = run_rolling_drain(**rolling_args)
            counts = payload.get("error_counts")
            session_code = next(
                (
                    code
                    for code in (*sorted(session_alert.PRODUCTION_ERROR_CODES), *_SESSION_ALERT_PRIORITY)
                    if isinstance(counts, dict)
                    and isinstance(counts.get(code), int)
                    and not isinstance(counts.get(code), bool)
                    and counts[code] > 0
                ),
                "",
            )
            non_retry_counts = {
                code: count
                for code, count in (counts or {}).items()
                if code != "codex_review_retry_pending"
            }
            if not session_code and (
                payload.get("status") not in {"completed", "completed_with_quarantine"}
                or any(
                    isinstance(n, int) and n > 0
                    for n in non_retry_counts.values()
                )
            ):
                session_code = "controller_failed"
            if session_code:
                payload["notification"] = session_alert.alert_from_environment(
                    session_code
                )
            elif payload.get("status") == "completed":
                reset = session_alert.clear_active_alert(session_only=True)
                if reset and reset.get("status") == "unknown":
                    payload["notification_reset"] = reset
    except Exception as error:
        session_code = (
            "" if args.read_only else _session_error_code(error)
        )
        if (
            controller_started
            and not session_code
            and str(error) == "production_rolling_controller_active"
        ):
            handoff = _verified_active_controller()
            if handoff is not None:
                print(json.dumps({
                    "schema_version": 1,
                    "kind": "preaudit_production_rolling_drain",
                    "status": "skipped",
                    "reason": "production_rolling_controller_active",
                    "controller_handoff": handoff,
                }, ensure_ascii=False, sort_keys=True))
                return 0
        error_code = session_code or str(error)
        if not session_code:
            error_prefix = error_code.partition(":")[0].strip()
            if error_prefix in session_alert.PRODUCTION_ERROR_CODES:
                error_code = error_prefix
        if not error_code.replace("_", "").isalnum() or not error_code.isascii():
            error_code = "controller_failed" if controller_started else "production_probe_failed"
        payload = {
            "schema_version": 1,
            "kind": (
                "preaudit_read_only_concurrency_probe_failed"
                if args.read_only
                else "preaudit_production_concurrency_probe_failed"
            ),
            "error_code": error_code,
        }
        if (
            isinstance(error, ControllerChildrenActiveError)
            and error.primary_error_code
        ):
            payload["primary_error_code"] = error.primary_error_code
        if controller_started:
            failure_code = str(error).partition(":")[0].strip()
            alert_code = session_code or (
                failure_code if failure_code in session_alert.PRODUCTION_ERROR_CODES
                else "controller_failed"
            )
            payload["notification"] = session_alert.alert_from_environment(alert_code)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("status") != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
