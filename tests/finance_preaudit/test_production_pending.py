from __future__ import annotations

import pytest

from apps.oa_finance_preaudit import production_pending


TRAVEL_CONFIG = {
    "flow_key": "travel_expense",
    "workflow_id": "138023",
    "workflow_name": "差旅费报销流程",
}


def _row(
    request_id: str = "recorded-request",
    *,
    workflow_id: str = "138023",
    workflow_name: str = "差旅费报销流程",
    received_at: str = "2026-08-22T09:00:00+08:00",
) -> dict:
    return {
        "request_id": request_id,
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "received_at": received_at,
    }


def test_supported_pending_identity_preserves_received_at() -> None:
    received_at = "2026-08-22T09:03:21+08:00"

    items = production_pending.identify_pending_rows(
        [_row(received_at=received_at)],
        [TRAVEL_CONFIG],
    )

    assert items == [
        {
            "requestId": "recorded-request",
            "received_at": received_at,
            "workflow_id": "138023",
            "workflow_name": "差旅费报销流程",
            "flow_key": "travel_expense",
            "support_status": "supported",
            "can_submit": True,
        }
    ]


def test_unknown_pending_identity_is_not_submittable() -> None:
    item = production_pending.identify_pending_rows(
        [_row(workflow_id="999999", workflow_name="未知流程")],
        [TRAVEL_CONFIG],
    )[0]

    assert item["flow_key"] == ""
    assert item["support_status"] == "unsupported_workflow"
    assert item["can_submit"] is False


def test_ambiguous_pending_identity_is_not_submittable() -> None:
    duplicate_match = {
        "flow_key": "business_support_fee",
        "workflow_id": "138023",
        "workflow_name": "另一流程",
    }

    item = production_pending.identify_pending_rows(
        [_row()],
        [TRAVEL_CONFIG, duplicate_match],
    )[0]

    assert item["flow_key"] == ""
    assert item["support_status"] == "ambiguous_workflow_identity"
    assert item["can_submit"] is False


@pytest.mark.parametrize("request_ids", (("",), ("same", "same")))
def test_missing_or_duplicate_request_identity_is_rejected(
    request_ids: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="production_pending_identity_invalid"):
        production_pending.identify_pending_rows(
            [_row(request_id) for request_id in request_ids],
            [TRAVEL_CONFIG],
        )

