from __future__ import annotations

from typing import Any

from apps.oa_approval import flow


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
            raise ValueError("production_pending_identity_invalid")
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
