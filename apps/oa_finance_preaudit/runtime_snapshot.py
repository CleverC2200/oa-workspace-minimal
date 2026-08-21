from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping


class RuntimeSnapshotError(RuntimeError):
    pass


DEFAULT_ROLE_FILES: Mapping[str, tuple[str, ...]] = {
    "dispatcher": (
        ".agents/skills/oa-finance-preaudit/references/dispatcher-contract.md",
    ),
    "controller": (
        ".agents/skills/oa-finance-preaudit/references/controller-contract.md",
        "apps/oa_approval/api.py",
        "apps/oa_approval/auth.py",
        "apps/oa_approval/configs/business_entertainment.json",
        "apps/oa_approval/configs/business_support_fee.json",
        "apps/oa_approval/configs/packaging_layout_approval.json",
        "apps/oa_approval/configs/small_marketing_fee.json",
        "apps/oa_approval/configs/travel_expense.json",
        "apps/oa_approval/flow.py",
        "apps/oa_finance_preaudit/elastic_queue.py",
        "apps/oa_finance_preaudit/json_artifact.py",
        "apps/oa_finance_preaudit/live_inputs.py",
        "apps/oa_finance_preaudit/production_pending.py",
        "apps/oa_finance_preaudit/queue_cli.py",
        "apps/oa_finance_preaudit/role_cli.py",
        "apps/oa_finance_preaudit/runtime_snapshot.py",
    ),
    "reviewer": (
        ".agents/skills/oa-finance-preaudit/SKILL.md",
        ".agents/skills/oa-finance-preaudit/references/flows/business-entertainment.md",
        ".agents/skills/oa-finance-preaudit/references/flows/business-support-fee.md",
        ".agents/skills/oa-finance-preaudit/references/flows/packaging-layout-approval.md",
        ".agents/skills/oa-finance-preaudit/references/flows/small-marketing-fee.md",
        ".agents/skills/oa-finance-preaudit/references/flows/travel-expense.md",
        ".agents/skills/oa-finance-preaudit/references/reviewer-contract.md",
        ".agents/skills/oa-finance-preaudit/references/result-contract.md",
        ".agents/skills/oa-finance-preaudit/references/observation-contract.md",
        ".agents/skills/audit-chinese-image-text/SKILL.md",
        ".agents/skills/audit-chinese-image-text/references/error-taxonomy.md",
        ".agents/skills/audit-chinese-image-text/references/output-contract.md",
        ".agents/skills/audit-chinese-image-text/references/problem-set.json",
        ".agents/skills/audit-chinese-image-text/references/visual-agent-prompts.md",
        ".agents/skills/audit-chinese-image-text/scripts/problem_set.py",
        "apps/oa_approval/api.py",
        "apps/oa_approval/attachment_extractors.py",
        "apps/oa_approval/attachments.py",
        "apps/oa_approval/configs/packaging_layout_approval.json",
        "apps/oa_approval/evidence_packet.py",
        "apps/oa_approval/resource_slots.py",
        "apps/oa_approval/travel_orders.py",
        "apps/oa_finance_preaudit/case_bundle.py",
        "apps/oa_finance_preaudit/cli.py",
        "apps/oa_finance_preaudit/elastic_queue.py",
        "apps/oa_finance_preaudit/json_artifact.py",
        "apps/oa_finance_preaudit/queue_cli.py",
        "apps/oa_finance_preaudit/review_attempt.py",
        "apps/oa_finance_preaudit/role_cli.py",
    ),
    "writer": (
        ".agents/skills/oa-finance-preaudit/references/writer-contract.md",
        "apps/oa_finance_preaudit/elastic_queue.py",
        "apps/oa_finance_preaudit/json_artifact.py",
        "apps/oa_finance_preaudit/live_inputs.py",
        "apps/oa_finance_preaudit/queue_cli.py",
        "apps/oa_finance_preaudit/role_cli.py",
    ),
}

DEFAULT_WRITER_RUNTIME_FILES = (
    "apps/oa_approval/api.py",
    "apps/oa_approval/auth.py",
    "apps/oa_approval/frozen_writer.py",
    "apps/oa_approval/runtime_control.py",
    "apps/oa_approval/writer_api.py",
)


def _field_value(line: str, expected_key: str) -> str:
    for field in line.split("|")[1:]:
        key, separator, value = field.partition("=")
        if separator and key == expected_key:
            return value.strip()
    return ""


def _event_id(line: str) -> str:
    return _field_value(line, "event_id")


def reconcile_ops(memory_text: str) -> dict:
    opened: set[str] = set()
    resolved: set[str] = set()
    legacy_opened: list[str] = []
    legacy_resolved: set[str] = set()
    for raw_line in str(memory_text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("OPEN_OP|"):
            event_id = _event_id(line)
            if event_id:
                opened.add(event_id)
            else:
                ordinal = len(legacy_opened) + 1
                legacy_opened.append(
                    hashlib.sha256(f"{ordinal}\0{line}".encode("utf-8")).hexdigest()
                )
        elif line.startswith("OPS_RESOLUTION|"):
            event_id = _event_id(line)
            if event_id:
                resolved.add(event_id)
            legacy_key = _field_value(line, "legacy_open_op_sha256")
            if legacy_key:
                legacy_resolved.add(legacy_key)
    unresolved = sorted(opened - resolved)
    unresolved_legacy = [
        value for value in legacy_opened if value not in legacy_resolved
    ]
    return {
        "unresolved_event_ids": unresolved,
        "unresolved_count": len(unresolved),
        "missing_event_id_count": len(unresolved_legacy),
        "unresolved_legacy_open_op_sha256": unresolved_legacy,
        "closure": (
            "closed"
            if not unresolved and not unresolved_legacy
            else "awaiting_ops_resolution"
        ),
    }


def _role_hash(root: Path, files: Iterable[str]) -> dict:
    digest = hashlib.sha256()
    normalized: list[str] = []
    for relative in sorted(str(value) for value in files):
        path = (root / relative).resolve()
        try:
            path.relative_to(root.resolve())
            content = path.read_bytes()
        except (OSError, ValueError) as error:
            raise RuntimeSnapshotError("release_file_unreadable") from error
        normalized.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return {"sha256": digest.hexdigest(), "files": normalized}


def build_release_manifest(
    project_root: Path,
    *,
    role_files: Mapping[str, tuple[str, ...]] = DEFAULT_ROLE_FILES,
    writer_root: Path | None = None,
    writer_runtime_files: tuple[str, ...] = DEFAULT_WRITER_RUNTIME_FILES,
) -> dict:
    root = Path(project_root).expanduser().resolve()
    roles = {
        role: _role_hash(root, files)
        for role, files in sorted(role_files.items())
    }
    if writer_root is not None and "writer" in roles:
        runtime = _role_hash(
            Path(writer_root).expanduser().resolve(),
            writer_runtime_files,
        )
        combined = hashlib.sha256()
        combined.update(bytes.fromhex(roles["writer"]["sha256"]))
        combined.update(bytes.fromhex(runtime["sha256"]))
        roles["writer"]["sha256"] = combined.hexdigest()
        roles["writer"]["runtime_files"] = runtime["files"]
    return {
        "schema_version": 1,
        "kind": "preaudit_release_manifest",
        "roles": roles,
    }


def build_role_release(
    project_root: Path,
    role: str,
    *,
    writer_root: Path | None = None,
) -> dict:
    name = str(role or "").strip()
    files = DEFAULT_ROLE_FILES.get(name)
    if files is None:
        raise RuntimeSnapshotError("release_role_invalid")
    if name == "writer" and writer_root is None:
        raise RuntimeSnapshotError("release_writer_root_missing")
    manifest = build_release_manifest(
        project_root,
        role_files={name: files},
        writer_root=writer_root if name == "writer" else None,
    )
    return {
        "schema_version": 1,
        "kind": "preaudit_role_release",
        "role": name,
        **manifest["roles"][name],
    }


def build_runtime_snapshot(
    *,
    queue_status: dict,
    production_status: dict,
    ops_status: dict,
    release_manifest: dict,
    now: datetime | None = None,
) -> dict:
    runtime_control = production_status.get("runtime_control") or {}
    active_run = runtime_control.get("active_run") or {}
    summary = queue_status.get("summary") or {}
    pool = queue_status.get("pool") or {}
    workers = queue_status.get("active_workers") or []
    refreshed_at = str(queue_status.get("refreshed_at") or "").strip()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    age_seconds = 0
    due_by_time = not refreshed_at
    if refreshed_at:
        normalized = (
            f"{refreshed_at[:-1]}+00:00"
            if refreshed_at.endswith("Z")
            else refreshed_at
        )
        try:
            refreshed = datetime.fromisoformat(normalized)
            if refreshed.tzinfo is None:
                refreshed = refreshed.replace(tzinfo=timezone.utc)
            age_seconds = max(
                0,
                int((current - refreshed.astimezone(timezone.utc)).total_seconds()),
            )
            due_by_time = age_seconds >= 30 * 60
        except ValueError:
            due_by_time = True
    attempts_closed = int(queue_status.get("attempts_closed_since_refresh") or 0)
    due_by_attempts = attempts_closed >= 10
    last_refreshed_intersection = int(summary.get("total") or 0)
    actionable = int(summary.get("actionable") or 0)
    blocked = int(summary.get("blocked") or 0)
    if actionable > 0:
        candidate_state = "actionable"
    elif last_refreshed_intersection == 0:
        candidate_state = "empty"
    elif blocked == last_refreshed_intersection:
        candidate_state = "all_candidates_blocked"
    else:
        candidate_state = "nonactionable"
    ops_closure = str(ops_status.get("closure") or "awaiting_ops_resolution")
    if candidate_state == "all_candidates_blocked":
        ops_closure = "awaiting_ops_resolution"
    return {
        "schema_version": 1,
        "kind": "preaudit_runtime_snapshot",
        "queue": {
            "generation": int(queue_status.get("generation") or 0),
            "last_refreshed_intersection": last_refreshed_intersection,
            "actionable": actionable,
            "blocked": blocked,
            "candidate_state": candidate_state,
            "travel": int(summary.get("travel") or 0),
            "nontravel": int(summary.get("nontravel") or 0),
            "oldest_actionable_age_seconds": int(
                summary.get("oldest_actionable_age_seconds") or 0
            ),
            "queue_age_p50_seconds": int(summary.get("queue_age_p50_seconds") or 0),
            "queue_age_p95_seconds": int(summary.get("queue_age_p95_seconds") or 0),
            "fifo_head_age_seconds": int(summary.get("fifo_head_age_seconds") or 0),
            "fifo_head_lane": str(summary.get("fifo_head_lane") or ""),
            "prepared_total": int(summary.get("prepared_total") or 0),
            "contiguous_prepared_depth": int(
                summary.get("contiguous_prepared_depth") or 0
            ),
            "lane_oldest_preparation_age_seconds": dict(
                summary.get("lane_oldest_preparation_age_seconds") or {}
            ),
            "states": dict(queue_status.get("states") or {}),
            "next_submit": dict(queue_status.get("next_submit") or {}),
        },
        "reviewer_pool": {
            "desired": int(pool.get("desired_workers") or 0),
            "max": int(pool.get("max_workers") or 0),
            "queue_active": len(workers),
            "ordinary_desired": int(pool.get("ordinary_desired_workers") or 0),
            "boost_eligible": pool.get("boost_eligible") is True,
            "reason_codes": list(pool.get("reason_codes") or []),
            "lane_priority": list(pool.get("lane_priority") or []),
            "lanes": dict(pool.get("lanes") or {}),
        },
        "refresh_checkpoint": {
            "due": due_by_time or due_by_attempts,
            "due_by_time": due_by_time,
            "due_by_attempts": due_by_attempts,
            "refreshed_at": refreshed_at,
            "age_seconds": age_seconds,
            "attempts_closed_since_refresh": attempts_closed,
        },
        "production": {
            "ok": production_status.get("ok") is True,
            "active_run": active_run.get("active") is True,
            "stop_requested": runtime_control.get("stop_requested") is True,
        },
        "ops": {
            "unresolved_count": int(ops_status.get("unresolved_count") or 0),
            "missing_event_id_count": int(
                ops_status.get("missing_event_id_count") or 0
            ),
            "closure": ops_closure,
        },
        "release": release_manifest,
    }


def read_runtime_snapshot(
    *,
    queue_status: dict,
    production_status_file: Path,
    ops_memory_file: Path,
    project_root: Path,
    writer_root: Path | None = None,
) -> dict:
    try:
        production_status = json.loads(
            Path(production_status_file).read_text(encoding="utf-8")
        )
        memory_text = Path(ops_memory_file).read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeSnapshotError("runtime_snapshot_input_unreadable") from error
    if not isinstance(production_status, dict):
        raise RuntimeSnapshotError("runtime_snapshot_input_invalid")
    return build_runtime_snapshot(
        queue_status=queue_status,
        production_status=production_status,
        ops_status=reconcile_ops(memory_text),
        release_manifest=build_release_manifest(
            project_root,
            writer_root=writer_root,
        ),
    )
