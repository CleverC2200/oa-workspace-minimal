from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from apps.oa_approval import flow


_OA_TIMEZONE = ZoneInfo("Asia/Shanghai")


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


def actionable_fifo(
    items: list[dict[str, Any]],
    *,
    excluded_identities: (
        set[tuple[str, str]] | frozenset[tuple[str, str]]
    ) = frozenset(),
    allowed_workflows: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Return supported writable rows in stable OA operation-time order."""

    ordered: list[tuple[datetime, str, dict[str, Any]]] = []
    for item in items:
        request_id = str(item.get("requestId") or "").strip()
        received_at = str(item.get("received_at") or "").strip()
        if item.get("support_status") == "supported" and (
            not request_id or not received_at
        ):
            raise ValueError("production_pending_identity_invalid")
        if (
            item.get("support_status") != "supported"
            or item.get("can_submit") is not True
            or (
                allowed_workflows is not None
                and item.get("flow_key") not in allowed_workflows
            )
            or (request_id, received_at) in excluded_identities
        ):
            continue
        try:
            timestamp = datetime.fromisoformat(
                received_at.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError("production_pending_received_at_invalid") from error
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=_OA_TIMEZONE)
        ordered.append(
            (timestamp.astimezone(timezone.utc), request_id, item)
        )
    return [item for _timestamp, _request_id, item in sorted(ordered)]
