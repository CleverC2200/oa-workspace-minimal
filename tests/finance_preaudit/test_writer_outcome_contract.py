from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import flow_engine, production_write


NOW = datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)


def _material(request_id: str) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_material",
        "request_id": request_id,
        "workflow_key": "travel_expense",
        "workflow_name": "差旅费报销流程",
        "declared_fields": [],
        "resources": [],
        "prepared": {"images": [], "structured_contents": [], "manifest": []},
        "authority_facts": [],
        "warnings": [],
    }


def _decision(action: dict) -> dict:
    packet = json.loads(Path(action["review_packet_file"]).read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "action_token": action["action_token"],
        "workflow_key": "travel_expense",
        "rule_version": packet["rule_version"],
        "overall": "pass",
        "items": [
            {
                "item_key": item["item_key"],
                "status": "pass",
                "conclusion": "证据符合当前审核项。",
                "evidence_refs": ["material:declared_fields"],
                "missing_evidence": [],
            }
            for item in packet["items"]
        ],
    }


class _WriterApi:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def pending_rows(self, **_kwargs) -> list[dict]:
        return [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
            }
        ]

    def submit_and_verify(self, request_id: str, remark: str) -> dict:
        self.calls.append((request_id, remark))
        return dict(self.result)


@pytest.mark.parametrize(
    ("failure_code", "submit_ok", "expected_write_state"),
    (
        ("writer_submit_rejected", False, "not_requested"),
        ("writer_transport_unknown", False, "unknown"),
        ("writer_postcheck_timeout", True, "unknown"),
        ("writer_pending_conflict", True, "unknown"),
    ),
)
def test_controlled_write_preserves_writer_failure_classification(
    tmp_path: Path,
    failure_code: str,
    submit_ok: bool,
    expected_write_state: str,
) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    api = _WriterApi(
        {
            "submit_ok": submit_ok,
            "done_hit": False,
            "postcheck_done_hit": False,
            "failure_code": failure_code,
        }
    )
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: NOW,
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["action"] == "needs_admin"
    assert result["reason"] == failure_code
    assert result["write_state"] == expected_write_state
    assert len(api.calls) == 1
