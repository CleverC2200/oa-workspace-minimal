from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from . import observability, retention


SCHEMA_VERSION = 1
_WORKFLOW_KEYS = {
    "travel_expense",
    "business_entertainment",
    "business_support_fee",
    "small_marketing_fee",
    "packaging_layout_approval",
}
_FAILURE_CODES = {
    "frozen_material_changed",
    "frozen_packet_changed",
    "material_collection_failed",
    "observability_incomplete",
    "observability_write_failed",
    "persistent_state_invalid",
    "state_invalid",
    "submission_adapter_unavailable",
    "write_authorization_invalid",
    "writer_result_unknown",
}
_ATTEMPT_PATH = re.compile(
    r"^(?:\.archive/)?(?:travel_expense|business_entertainment|"
    r"business_support_fee|small_marketing_fee|packaging_layout_approval)/"
    r"cases/[0-9a-f]{64}/attempt-[1-9][0-9]*$"
)
_PLAN_MAX_AGE = timedelta(minutes=5)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("runtime_state_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("runtime_state_invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("runtime_state_invalid")
    return parsed


def _read_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("runtime_state_invalid") from error
    attempt_root = path.parent
    try:
        expected_attempt = int(attempt_root.name.removeprefix("attempt-"))
    except ValueError as error:
        raise ValueError("runtime_state_invalid") from error
    expected_case = attempt_root.parent.name
    expected_workflow = attempt_root.parent.parent.parent.name
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != SCHEMA_VERSION
        or state.get("kind") != "preaudit_action"
        or state.get("workflow_key") not in _WORKFLOW_KEYS
        or state.get("execution_state")
        not in {"collecting", "review_required", "needs_admin", "closed"}
        or state.get("business_state")
        not in {"not_available", "pass", "needs_human"}
        or state.get("write_state")
        not in {"not_requested", "authorized", "submitting", "submitted", "unknown"}
        or not isinstance(state.get("case_key"), str)
        or not isinstance(state.get("attempt"), int)
        or isinstance(state.get("attempt"), bool)
        or state["attempt"] < 1
        or state.get("workflow_key") != expected_workflow
        or state.get("case_key") != expected_case
        or state.get("attempt") != expected_attempt
        or not re.fullmatch(r"[0-9a-f]{64}", expected_case)
    ):
        raise ValueError("runtime_state_invalid")
    _timestamp(state.get("created_at"))
    return state


def _item_key(case_key: str, attempt: int) -> str:
    raw = f"{case_key}\x1f{attempt}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _attempt_roots(root: Path, *, archived: bool = False) -> list[Path]:
    base = root / ".archive" if archived else root
    if base.is_symlink():
        raise ValueError("runtime_path_symlink")
    paths: list[Path] = []
    for workflow_key in sorted(_WORKFLOW_KEYS):
        workflow_root = base / workflow_key
        cases_root = workflow_root / "cases"
        if workflow_root.is_symlink() or cases_root.is_symlink():
            raise ValueError("runtime_path_symlink")
        if not cases_root.is_dir():
            continue
        for case_root in cases_root.iterdir():
            if case_root.is_symlink():
                raise ValueError("runtime_path_symlink")
            if not case_root.is_dir():
                continue
            for attempt_root in case_root.glob("attempt-*"):
                if attempt_root.is_symlink():
                    raise ValueError("runtime_path_symlink")
                if not attempt_root.is_dir():
                    continue
                if any(path.is_symlink() for path in attempt_root.rglob("*")):
                    raise ValueError("runtime_path_symlink")
                paths.append(attempt_root)
    return sorted(paths, key=str)


def _age_seconds(created_at: datetime, now: datetime) -> int:
    return max(0, int((now - created_at).total_seconds()))


def status(root: Path, *, now: datetime | None = None) -> dict:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("runtime_now_invalid")
    runtime_root = Path(root).expanduser()
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise ValueError("runtime_root_invalid")

    by_workflow: dict[str, list[dict]] = {}
    attempt_roots = _attempt_roots(runtime_root)
    metric_cutoff = current_time - timedelta(days=retention.RETENTION_DAYS)
    archived_metric_roots: list[Path] = []
    for attempt_root in _attempt_roots(runtime_root, archived=True):
        try:
            state = _read_state(attempt_root / "state.json")
            created_at = _timestamp(state["created_at"])
        except ValueError:
            continue
        if created_at >= metric_cutoff:
            archived_metric_roots.append(attempt_root)
    metric_roots = [
        *attempt_roots,
        *archived_metric_roots,
    ]
    for attempt_root in attempt_roots:
        try:
            state = _read_state(attempt_root / "state.json")
        except ValueError:
            state_path = attempt_root / "state.json"
            created_at = datetime.fromtimestamp(
                state_path.stat().st_mtime if state_path.exists() else 0,
                tz=timezone.utc,
            )
            workflow_key = attempt_root.parent.parent.parent.name
            try:
                attempt = int(attempt_root.name.removeprefix("attempt-"))
            except ValueError:
                attempt = 0
            state = {
                "workflow_key": workflow_key,
                "case_key": attempt_root.parent.name,
                "attempt": attempt,
                "execution_state": "needs_admin",
                "business_state": "not_available",
                "write_state": "unknown",
                "reason": "state_invalid",
            }
        else:
            created_at = _timestamp(state["created_at"])
        reason = str(state.get("reason") or "")
        failure_code = (
            reason
            if reason in _FAILURE_CODES
            else ("unknown_failure" if reason else None)
        )
        by_workflow.setdefault(state["workflow_key"], []).append(
            {
                "item_key": _item_key(state["case_key"], state["attempt"]),
                "case_key": state["case_key"],
                "attempt": state["attempt"],
                "created_at": created_at,
                "age_seconds": _age_seconds(created_at, current_time),
                "execution_state": state["execution_state"],
                "business_state": state["business_state"],
                "write_state": state["write_state"],
                "failure_code": failure_code,
            }
        )

    workflows: list[dict] = []
    for workflow_key, attempts in sorted(by_workflow.items()):
        attempts.sort(key=lambda item: (item["created_at"], item["item_key"]))
        latest = attempts[-1]
        open_attempts = [
            item for item in attempts if item["execution_state"] != "closed"
        ]
        execution_states: dict[str, int] = {}
        business_states: dict[str, int] = {}
        write_states: dict[str, int] = {}
        failure_counts: dict[str, int] = {}
        for item in attempts:
            for counts, key in (
                (execution_states, item["execution_state"]),
                (business_states, item["business_state"]),
                (write_states, item["write_state"]),
            ):
                counts[key] = counts.get(key, 0) + 1
            if item["failure_code"]:
                code = item["failure_code"]
                failure_counts[code] = failure_counts.get(code, 0) + 1
        workflows.append(
            {
                "workflow_key": workflow_key,
                "latest_round": {
                    key: latest[key]
                    for key in (
                        "item_key",
                        "attempt",
                        "age_seconds",
                        "execution_state",
                        "business_state",
                        "write_state",
                    )
                },
                "case_count": len({item["case_key"] for item in attempts}),
                "attempt_count": len(attempts),
                "backlog_count": len(open_attempts),
                "oldest_backlog_age_seconds": (
                    max(item["age_seconds"] for item in open_attempts)
                    if open_attempts
                    else 0
                ),
                "execution_states": execution_states,
                "business_states": business_states,
                "write_states": write_states,
                "failure_counts": failure_counts,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_runtime_status",
        "generated_at": current_time.isoformat(),
        "workflows": workflows,
        "observability": observability.summarize_logs(
            path
            for path in (
                attempt_root / "observability.json"
                for attempt_root in metric_roots
            )
            if path.is_file()
        ),
    }


def _plan_fingerprint(plan: dict) -> str:
    payload = {key: value for key, value in plan.items() if key != "fingerprint"}
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def build_prune_plan(
    root: Path,
    *,
    now: datetime | None = None,
    retention_days: int = retention.RETENTION_DAYS,
) -> dict:
    current_time = now or datetime.now(timezone.utc)
    if (
        current_time.tzinfo is None
        or isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or retention_days != retention.RETENTION_DAYS
    ):
        raise ValueError("retention_plan_invalid")
    raw_root = Path(root).expanduser()
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise ValueError("runtime_root_invalid")
    runtime_root = raw_root.resolve(strict=True)
    cutoff = current_time - timedelta(days=retention_days)
    active: list[tuple[Path, dict | None]] = []
    for attempt_root in _attempt_roots(runtime_root):
        try:
            state = _read_state(attempt_root / "state.json")
        except ValueError:
            state = None
        active.append((attempt_root, state))
    latest_by_workflow: dict[str, tuple[datetime, Path]] = {}
    for attempt_root, state in active:
        if state is None:
            continue
        created_at = _timestamp(state["created_at"])
        current = latest_by_workflow.get(state["workflow_key"])
        if current is None or (created_at, str(attempt_root)) > (
            current[0],
            str(current[1]),
        ):
            latest_by_workflow[state["workflow_key"]] = (
                created_at,
                attempt_root,
            )

    actions: list[dict] = []
    for attempt_root, state in active:
        relative_path = str(attempt_root.relative_to(runtime_root))
        if state is None:
            action, reason = "keep", "state_invalid"
        else:
            created_at = _timestamp(state["created_at"])
            is_latest = latest_by_workflow.get(state["workflow_key"], (None, None))[1] == attempt_root
            if state["execution_state"] != "closed":
                action, reason = "keep", "not_closed"
            elif state["write_state"] == "unknown":
                action, reason = "keep", "write_state_unknown"
            elif is_latest:
                action, reason = "keep", "latest_task"
            elif created_at < cutoff:
                action, reason = "delete_path", "expired"
            else:
                action, reason = "archive_path", "previous_task"
        actions.append(
            {
                "key": hashlib.sha256(relative_path.encode("utf-8")).hexdigest(),
                "action": action,
                "reason": reason,
                "relative_path": relative_path,
            }
        )

    for attempt_root in _attempt_roots(runtime_root, archived=True):
        relative_from_archive = attempt_root.relative_to(runtime_root / ".archive")
        relative_path = str(Path(".archive") / relative_from_archive)
        try:
            state = _read_state(attempt_root / "state.json")
        except ValueError:
            state = None
        if state is None:
            action, reason = "keep", "state_invalid"
        else:
            created_at = _timestamp(state["created_at"])
            if state["execution_state"] != "closed":
                action, reason = "keep", "not_closed"
            elif state["write_state"] == "unknown":
                action, reason = "keep", "write_state_unknown"
            elif created_at < cutoff:
                action, reason = "delete_path", "expired"
            else:
                action, reason = "keep", "within_retention"
        actions.append(
            {
                "key": hashlib.sha256(relative_path.encode("utf-8")).hexdigest(),
                "action": action,
                "reason": reason,
                "relative_path": relative_path,
            }
        )

    actions.sort(key=lambda item: item["relative_path"])
    plan = {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_retention_plan",
        "generated_at": current_time.isoformat(),
        "retention_days": retention_days,
        "actions": actions,
    }
    return {**plan, "fingerprint": _plan_fingerprint(plan)}


def _validated_plan(plan: object) -> list[retention.RetentionAction]:
    if (
        not isinstance(plan, dict)
        or set(plan)
        != {
            "schema_version",
            "kind",
            "generated_at",
            "retention_days",
            "actions",
            "fingerprint",
        }
        or plan.get("schema_version") != SCHEMA_VERSION
        or plan.get("kind") != "preaudit_retention_plan"
        or not isinstance(plan.get("retention_days"), int)
        or isinstance(plan.get("retention_days"), bool)
        or plan["retention_days"] != retention.RETENTION_DAYS
        or not isinstance(plan.get("actions"), list)
        or plan.get("fingerprint") != _plan_fingerprint(plan)
    ):
        raise ValueError("retention_plan_invalid")
    try:
        _timestamp(plan.get("generated_at"))
    except ValueError as error:
        raise ValueError("retention_plan_invalid") from error
    actions: list[retention.RetentionAction] = []
    keys: set[str] = set()
    paths: set[str] = set()
    for item in plan["actions"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"key", "action", "reason", "relative_path"}
            or item.get("action") not in {"archive_path", "delete_path", "keep"}
            or not isinstance(item.get("key"), str)
            or not isinstance(item.get("reason"), str)
            or not isinstance(item.get("relative_path"), str)
            or not _ATTEMPT_PATH.fullmatch(item["relative_path"])
            or (
                item.get("action") == "archive_path"
                and item["relative_path"].startswith(".archive/")
            )
            or item["key"] in keys
            or item["relative_path"] in paths
            or item["key"]
            != hashlib.sha256(item["relative_path"].encode("utf-8")).hexdigest()
        ):
            raise ValueError("retention_plan_invalid")
        keys.add(item["key"])
        paths.add(item["relative_path"])
        actions.append(retention.RetentionAction(**item))
    return actions


def apply_prune_plan(
    root: Path,
    plan: object,
    *,
    now: datetime | None = None,
) -> dict:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("runtime_now_invalid")
    actions = _validated_plan(plan)
    try:
        with retention.maintenance_lock(root) as locked_root:
            retention.validate_file_actions(locked_root, actions)
            generated_at = _timestamp(plan["generated_at"])
            if (
                generated_at > current_time
                or current_time - generated_at > _PLAN_MAX_AGE
            ):
                raise ValueError("retention_plan_stale")
            canonical = build_prune_plan(
                locked_root,
                now=generated_at,
                retention_days=plan["retention_days"],
            )
            canonical_by_path = {
                item["relative_path"]: item for item in canonical["actions"]
            }
            planned_by_path = {
                action.relative_path: action for action in actions
            }
            archived_destinations = {
                str(Path(".archive") / str(action.relative_path))
                for action in actions
                if action.action == "archive_path"
            }
            for relative_path, expected in canonical_by_path.items():
                planned = planned_by_path.get(relative_path)
                if planned is None:
                    if relative_path in archived_destinations:
                        continue
                    raise ValueError("retention_plan_incomplete")
                if (
                    planned.action != expected["action"]
                    or planned.reason != expected["reason"]
                ):
                    raise ValueError("retention_plan_conflict")
            for relative_path in planned_by_path.keys() - canonical_by_path.keys():
                target = locked_root / relative_path
                if target.exists() or target.is_symlink():
                    raise ValueError("retention_plan_extra_target")
            raw = retention.apply_file_actions_locked(
                locked_root,
                actions,
                now=current_time,
            )
    except ValueError as error:
        raise ValueError("retention_plan_invalid") from error
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_prune_receipt",
        "archived": raw["archived"],
        "deleted": raw["deleted"],
        "missing": raw["missing"],
        "failed": raw["failed"],
        "status": raw["status"],
    }


def prune(root: Path, *, now: datetime | None = None) -> dict:
    current_time = now or datetime.now(timezone.utc)
    plan = build_prune_plan(root, now=current_time)
    return apply_prune_plan(root, plan, now=current_time)
