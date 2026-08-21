from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Callable

from . import elastic_queue
from . import json_artifact


RunFunction = Callable[..., subprocess.CompletedProcess]


def _read_object(path: Path) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise elastic_queue.QueueContractError("live_inputs_unreadable") from error
    if not isinstance(payload, dict):
        raise elastic_queue.QueueContractError("live_inputs_schema_invalid")
    return payload


def _rows(payload: dict, key: str) -> list[dict]:
    rows = payload.get(key)
    if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
        raise elastic_queue.QueueContractError("live_inputs_schema_invalid")
    return list(rows)


def _production_rows(payload: dict) -> list[dict]:
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "preaudit_production_pending_identities"
    ):
        raise elastic_queue.QueueContractError("live_inputs_schema_invalid")
    rows = _rows(payload, "items")
    total = payload.get("total")
    returned = payload.get("returned")
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or isinstance(returned, bool)
        or not isinstance(returned, int)
        or returned != len(rows)
        or total != returned
    ):
        raise elastic_queue.QueueContractError("live_inputs_schema_invalid")
    seen_request_ids: set[str] = set()
    for item in rows:
        request_id = item.get("requestId")
        received_at = item.get("received_at")
        workflow_id = item.get("workflow_id")
        workflow_name = item.get("workflow_name")
        flow_key = item.get("flow_key")
        support_status = item.get("support_status")
        can_submit = item.get("can_submit")
        if (
            not isinstance(request_id, str)
            or not request_id.strip()
            or request_id in seen_request_ids
            or not isinstance(received_at, str)
            or not isinstance(workflow_id, str)
            or not isinstance(workflow_name, str)
            or not isinstance(flow_key, str)
            or support_status
            not in {
                "supported",
                "unsupported_workflow",
                "ambiguous_workflow_identity",
            }
            or not isinstance(can_submit, bool)
        ):
            raise elastic_queue.QueueContractError("live_inputs_schema_invalid")
        seen_request_ids.add(request_id)
        if support_status == "supported":
            if flow_key not in elastic_queue.SUPPORTED_FLOWS or can_submit is not True:
                raise elastic_queue.QueueContractError("live_inputs_schema_invalid")
        elif flow_key or can_submit is not False:
            raise elastic_queue.QueueContractError("live_inputs_schema_invalid")
    return rows


def _pending_rows(payload: dict) -> list[dict]:
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "preaudit_pending_cases"
        or payload.get("order") != "fifo_submitted_at"
    ):
        raise elastic_queue.QueueContractError("live_inputs_schema_invalid")
    return _rows(payload, "cases")


def _source_rows(payload: dict, key: str) -> list[dict]:
    if key == "items":
        return _production_rows(payload)
    if key == "cases":
        return _pending_rows(payload)
    raise elastic_queue.QueueContractError("live_inputs_schema_invalid")


def _sha256(path: Path) -> str:
    try:
        return f"sha256:{hashlib.sha256(Path(path).read_bytes()).hexdigest()}"
    except OSError as error:
        raise elastic_queue.QueueContractError("live_inputs_unreadable") from error


def _write_json_atomic(path: Path, payload: dict) -> None:
    try:
        json_artifact.write_json_atomic(path, payload)
    except (OSError, TypeError, ValueError) as error:
        raise elastic_queue.QueueContractError("live_inputs_manifest_write_failed") from error


def _capture_source(
    command: list[str],
    *,
    cwd: Path,
    output: Path,
    receipt_kind: str,
    rows_key: str,
    run: RunFunction,
) -> tuple[dict, int]:
    try:
        completed = run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise elastic_queue.QueueContractError("live_inputs_capture_failed") from error
    try:
        receipt = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise elastic_queue.QueueContractError("live_inputs_receipt_invalid") from error
    if (
        completed.returncode != 0
        or not isinstance(receipt, dict)
        or receipt.get("kind") != receipt_kind
        or Path(str(receipt.get("output") or "")).expanduser().resolve()
        != output.resolve()
    ):
        raise elastic_queue.QueueContractError("live_inputs_receipt_invalid")
    payload = _read_object(output)
    count = len(_source_rows(payload, rows_key))
    receipt_count = receipt.get("count")
    if (
        isinstance(receipt_count, bool)
        or not isinstance(receipt_count, int)
        or receipt_count < 0
        or receipt_count != count
    ):
        raise elastic_queue.QueueContractError("live_inputs_receipt_invalid")
    return payload, count


def capture_live_inputs(
    snapshot_dir: Path,
    *,
    project_root: Path,
    production_root: Path,
    run: RunFunction = subprocess.run,
) -> dict:
    target = Path(snapshot_dir).expanduser()
    project = Path(project_root).expanduser().resolve()
    production = Path(production_root).expanduser().resolve()
    if not target.is_absolute():
        raise elastic_queue.QueueContractError("live_inputs_directory_invalid")
    target = target.resolve()
    if target.exists():
        raise elastic_queue.QueueContractError("live_inputs_directory_exists")
    if not project.is_dir() or not production.is_dir():
        raise elastic_queue.QueueContractError("live_inputs_root_invalid")
    try:
        target.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        raise elastic_queue.QueueContractError("live_inputs_directory_create_failed") from error

    production_list = target / "production-list.json"
    pending_list = target / "pending-list.json"
    _production_payload, production_count = _capture_source(
        [
            sys.executable,
            "-m",
            "apps.oa_finance_preaudit.role_cli",
            "controller",
            "list-production-pending",
            "--output",
            str(production_list),
        ],
        cwd=production,
        output=production_list,
        receipt_kind="preaudit_production_identities_written",
        rows_key="items",
        run=run,
    )
    _pending_payload, pending_count = _capture_source(
        [
            sys.executable,
            "-m",
            "apps.oa_finance_preaudit.cli",
            "list-pending",
            "--output",
            str(pending_list),
        ],
        cwd=project,
        output=pending_list,
        receipt_kind="preaudit_case_index_written",
        rows_key="cases",
        run=run,
    )
    manifest = {
        "schema_version": 1,
        "kind": "preaudit_live_inputs",
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "production_list": str(production_list.resolve()),
        "pending_list": str(pending_list.resolve()),
        "production_sha256": _sha256(production_list),
        "pending_sha256": _sha256(pending_list),
        "production_count": production_count,
        "pending_count": pending_count,
    }
    manifest_path = target / "live-inputs.json"
    _write_json_atomic(manifest_path, manifest)
    return read_live_inputs_manifest(manifest_path)


def read_live_inputs_manifest(
    path: Path,
    *,
    now: datetime | None = None,
    max_age_seconds: int = 300,
) -> dict:
    manifest_path = Path(path).expanduser()
    if not manifest_path.is_absolute() or manifest_path.name != "live-inputs.json":
        raise elastic_queue.QueueContractError("live_inputs_manifest_path_invalid")
    manifest_path = manifest_path.resolve()
    manifest = _read_object(manifest_path)
    if manifest.get("schema_version") != 1 or manifest.get("kind") != "preaudit_live_inputs":
        raise elastic_queue.QueueContractError("live_inputs_schema_invalid")
    captured_at = str(manifest.get("captured_at") or "").strip()
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise elastic_queue.QueueContractError("live_inputs_schema_invalid") from error
    if captured.tzinfo is None:
        raise elastic_queue.QueueContractError("live_inputs_schema_invalid")
    current = now or datetime.now(timezone.utc)
    age_seconds = (
        current.astimezone(timezone.utc) - captured.astimezone(timezone.utc)
    ).total_seconds()
    if age_seconds < -30 or age_seconds > max(1, int(max_age_seconds)):
        raise elastic_queue.QueueContractError("live_inputs_stale")
    production_list = Path(str(manifest.get("production_list") or "")).expanduser().resolve()
    pending_list = Path(str(manifest.get("pending_list") or "")).expanduser().resolve()
    if (
        production_list != manifest_path.parent / "production-list.json"
        or pending_list != manifest_path.parent / "pending-list.json"
    ):
        raise elastic_queue.QueueContractError("live_inputs_manifest_path_invalid")
    production = _read_object(production_list)
    pending = _read_object(pending_list)
    if (
        manifest.get("production_sha256") != _sha256(production_list)
        or manifest.get("pending_sha256") != _sha256(pending_list)
    ):
        raise elastic_queue.QueueContractError("live_inputs_source_changed")
    production_count = len(_production_rows(production))
    pending_count = len(_pending_rows(pending))
    recorded_production_count = manifest.get("production_count")
    recorded_pending_count = manifest.get("pending_count")
    if (
        isinstance(recorded_production_count, bool)
        or not isinstance(recorded_production_count, int)
        or recorded_production_count < 0
        or recorded_production_count != production_count
        or isinstance(recorded_pending_count, bool)
        or not isinstance(recorded_pending_count, int)
        or recorded_pending_count < 0
        or recorded_pending_count != pending_count
    ):
        raise elastic_queue.QueueContractError("live_inputs_schema_invalid")
    return dict(manifest)
