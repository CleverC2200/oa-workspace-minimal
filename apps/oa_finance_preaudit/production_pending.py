from __future__ import annotations

from pathlib import Path
from typing import Any

from apps.oa_approval import api
from apps.oa_approval import flow

from . import elastic_queue
from apps import json_artifact


def _support_identity(
    row: dict[str, Any],
    configs: list[dict[str, Any]],
) -> tuple[str, str]:
    matches = [config for config in configs if flow.workflow_matches(row, config)]
    if len(matches) == 1:
        return str(matches[0].get("flow_key") or "").strip(), "supported"
    if len(matches) > 1:
        return "", "ambiguous_workflow_identity"
    return "", "unsupported_workflow"


def identify_pending_rows(
    rows: list[dict[str, Any]],
    configs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_request_ids: set[str] = set()
    for row in rows:
        request_id = str(row.get("request_id") or "").strip()
        if not request_id or request_id in seen_request_ids:
            raise elastic_queue.QueueContractError("production_pending_identity_invalid")
        seen_request_ids.add(request_id)
        workflow_id = str(row.get("workflow_id") or "").strip()
        workflow_name = str(row.get("workflow_name") or "").strip()
        flow_key, support_status = _support_identity(
            {
                "workflowId": workflow_id,
                "workflowName": workflow_name,
            },
            configs,
        )
        items.append(
            {
                "requestId": request_id,
                "received_at": str(row.get("received_at") or "").strip(),
                "workflow_id": workflow_id,
                "workflow_name": workflow_name,
                "flow_key": flow_key,
                "support_status": support_status,
                "can_submit": support_status == "supported",
            }
        )
    return items


def capture_production_pending(
    output: Path,
    *,
    oa_uid: str = "",
    page_size: int = 200,
    max_pages: int = 100,
) -> dict[str, Any]:
    configs = flow.load_all_configs()
    resolved_oa_uid = str(oa_uid or flow.common_oa_uid(configs)).strip()
    try:
        oa_api = api.build_api(configs[0], oa_uid_override=resolved_oa_uid)
        rows = oa_api.pending_rows(
            page_size=max(1, int(page_size)),
            max_pages=max(1, int(max_pages)),
        )
    except Exception as error:
        raise elastic_queue.QueueContractError(
            "production_pending_fetch_failed"
        ) from error
    items = identify_pending_rows(rows, configs)
    payload = {
        "schema_version": 1,
        "kind": "preaudit_production_pending_identities",
        "total": len(rows),
        "returned": len(items),
        "items": items,
    }
    target = Path(output).expanduser().resolve()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        json_artifact.write_json_atomic(target, payload)
    except (OSError, TypeError, ValueError) as error:
        raise elastic_queue.QueueContractError(
            "production_pending_output_failed"
        ) from error
    return {
        "output": str(target),
        "count": len(items),
        "source_kind": payload["kind"],
    }
