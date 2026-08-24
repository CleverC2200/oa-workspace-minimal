from __future__ import annotations

from contextlib import contextmanager
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


def _material_with_files(request_id: str, attempt_root: Path) -> dict:
    attachment_root = attempt_root / "attachments"
    prepared_root = attempt_root / "prepared"
    attachment_root.mkdir(parents=True, exist_ok=True)
    prepared_root.mkdir(parents=True, exist_ok=True)
    attachment = attachment_root / "source.txt"
    page = prepared_root / "page-1.txt"
    attachment.write_text("frozen attachment", encoding="utf-8")
    page.write_text("frozen page", encoding="utf-8")
    return {
        **_material(request_id),
        "resources": [
            {
                "request_id": request_id,
                "resource_id": "resource-1",
                "source_resource_id": "source-1",
                "source_system": "oa",
                "local_readonly_path": str(attachment),
            }
        ],
        "prepared": {
            "images": [
                {
                    "resource_id": "page-1",
                    "parent_resource_id": "resource-1",
                    "path": str(page),
                }
            ],
            "structured_contents": [],
            "manifest": [{"resource_id": "resource-1", "image_count": 1}],
        },
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


class FakeWriterApi:
    def __init__(
        self,
        *,
        received_at: str = "2026-08-23T08:30:00+08:00",
        on_pending=None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.received_at = received_at
        self.on_pending = on_pending

    def pending_rows(self, **_kwargs) -> list[dict]:
        if self.on_pending is not None:
            self.on_pending()
        return [
            {
                "request_id": "live-request",
                "received_at": self.received_at,
            }
        ]

    def submit_and_verify(self, request_id: str, remark: str) -> dict:
        self.calls.append((request_id, remark))
        return {
            "submit_ok": True,
            "done_hit": True,
            "postcheck_done_hit": True,
        }


def test_authorized_travel_sample_uses_one_bound_submit_and_receipt(
    tmp_path: Path,
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
    api = FakeWriterApi()
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
    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert action["mode"] == "controlled_write"
    assert action["write_authorized"] is True
    assert closed["write_state"] == "submitted"
    assert len(api.calls) == 1
    receipt = json.loads(Path(closed["receipt_file"]).read_text(encoding="utf-8"))
    assert receipt["write"] is True
    assert receipt["submit_ok"] is True
    assert receipt["done_hit"] is True
    assert receipt["postcheck_done_hit"] is True


def test_policy_blocks_an_unapproved_flow_before_submit(tmp_path: Path) -> None:
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    ports = production_write.build_ports(
        root=tmp_path / "flows",
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=FakeWriterApi,
        now=lambda: NOW,
    )
    engine = flow_engine.FlowEngine(
        root=tmp_path / "flows",
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "other-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: {
            **_material(request_id),
            "workflow_key": "business_entertainment",
        },
        **ports,
    )

    result = engine.next_action("business_entertainment", mode="controlled_write")

    assert result["action"] == "needs_admin"
    assert result["reason"] == "write_authorization_invalid"


def test_changed_live_version_stops_without_calling_submit(tmp_path: Path) -> None:
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
    api = FakeWriterApi(received_at="2026-08-23T08:31:00+08:00")
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
    assert result["reason"] == "writer_result_unknown"
    assert result["write_state"] == "unknown"
    assert api.calls == []


def test_authorization_expiring_while_waiting_for_writer_stops_submit(
    tmp_path: Path,
    monkeypatch,
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
    clock = [NOW]
    api = FakeWriterApi()
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: clock[0],
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

    @contextmanager
    def expire_while_waiting():
        clock[0] = NOW.replace(hour=2, minute=1)
        yield

    monkeypatch.setattr(
        production_write.runtime_control,
        "global_writer_lease",
        expire_while_waiting,
    )

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["action"] == "needs_admin"
    assert result["write_state"] == "unknown"
    assert api.calls == []


def test_evidence_changed_while_waiting_for_writer_stops_submit(
    tmp_path: Path,
    monkeypatch,
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
    api = FakeWriterApi()
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
        collect_material=_material_with_files,
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    @contextmanager
    def mutate_while_waiting():
        state = json.loads(Path(action["state_file"]).read_text(encoding="utf-8"))
        material = json.loads(Path(state["material_file"]).read_text(encoding="utf-8"))
        Path(material["prepared"]["images"][0]["path"]).write_text(
            "changed page",
            encoding="utf-8",
        )
        yield

    monkeypatch.setattr(
        production_write.runtime_control,
        "global_writer_lease",
        mutate_while_waiting,
    )

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["action"] == "needs_admin"
    assert result["write_state"] == "unknown"
    assert api.calls == []


def test_authorization_expiring_during_pending_query_stops_submit(
    tmp_path: Path,
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
    clock = [NOW]
    api = FakeWriterApi(on_pending=lambda: clock.__setitem__(0, NOW.replace(hour=2, minute=1)))
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: clock[0],
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
    assert result["write_state"] == "unknown"
    assert api.calls == []


def test_evidence_changed_during_pending_query_stops_submit(tmp_path: Path) -> None:
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
    api = FakeWriterApi()
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
        collect_material=_material_with_files,
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    state = json.loads(Path(action["state_file"]).read_text(encoding="utf-8"))
    material = json.loads(Path(state["material_file"]).read_text(encoding="utf-8"))
    page = Path(material["prepared"]["images"][0]["path"])
    api.on_pending = lambda: page.write_text("changed during query", encoding="utf-8")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["action"] == "needs_admin"
    assert result["write_state"] == "unknown"
    assert api.calls == []


@pytest.mark.parametrize("late_change", ("expire", "stop"))
def test_final_evidence_gate_rechecks_lightweight_controls(
    tmp_path: Path,
    monkeypatch,
    late_change: str,
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
    monkeypatch.delenv("OA_FINANCE_PREAUDIT_STOP_REQUESTED", raising=False)
    clock = [NOW]
    api = FakeWriterApi()
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: clock[0],
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
        collect_material=_material_with_files,
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    original_validate = flow_engine.validate_frozen_evidence
    validation_calls = 0

    def validate_then_change(**kwargs):
        nonlocal validation_calls
        result = original_validate(**kwargs)
        validation_calls += 1
        if validation_calls == 4:
            if late_change == "expire":
                clock[0] = NOW.replace(hour=2, minute=1)
            else:
                monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_REQUESTED", "1")
        return result

    monkeypatch.setattr(flow_engine, "validate_frozen_evidence", validate_then_change)

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert validation_calls == 4
    assert result["action"] == "needs_admin"
    assert result["write_state"] == "unknown"
    assert api.calls == []
