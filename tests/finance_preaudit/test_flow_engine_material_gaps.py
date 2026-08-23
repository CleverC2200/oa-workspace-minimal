from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import flow_engine


FINANCE_WORKFLOWS = (
    "travel_expense",
    "business_entertainment",
    "business_support_fee",
    "small_marketing_fee",
)


def _engine(
    root: Path,
    workflow_key: str,
    error: Exception,
    *,
    controlled: bool = False,
    submissions: list[dict] | None = None,
) -> flow_engine.FlowEngine:
    def collect_material(_request_id: str, _attempt_root: Path) -> dict:
        raise error

    def grant(context: dict) -> dict:
        return {
            "schema_version": 1,
            "kind": "preaudit_write_grant",
            "grant_id": "material-gap-test",
            **context,
            "mode": "controlled_write",
            "expires_at": "2026-08-23T12:00:00+08:00",
            "proof": "verified-by-material-gap-test",
        }

    def submit(job: dict) -> dict:
        if submissions is not None:
            submissions.append(dict(job))
        return {
            "schema_version": 1,
            "kind": "preaudit_submission_result",
            "submission_token": job["submission_token"],
            "write": True,
            "submit_ok": True,
            "done_hit": True,
            "postcheck_done_hit": True,
        }

    return flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda requested: [
            {
                "request_id": "material-gap-request",
                "received_at": "2026-08-23T09:00:00+08:00",
                "workflow_key": requested,
            }
        ],
        collect_material=collect_material,
        authorization_source=grant if controlled else None,
        authorization_verifier=(
            (lambda value, context: value == grant(context)) if controlled else None
        ),
        submit_frozen=submit if controlled else None,
    )


def _uncertain_decision(action: dict, packet: dict) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "action_token": action["action_token"],
        "workflow_key": action["workflow_key"],
        "rule_version": packet["rule_version"],
        "overall": "needs_human",
        "items": [
            {
                "item_key": item["item_key"],
                "status": "uncertain",
                "conclusion": (
                    f"{item['name']}的相关材料本次未能可信取得，"
                    "请财务结合原始单据判断。"
                ),
                "evidence_refs": [],
                "missing_evidence": ["当前审核项所需的可信材料"],
            }
            for item in packet["items"]
        ],
    }


@pytest.mark.parametrize("workflow_key", FINANCE_WORKFLOWS)
def test_finance_collection_failure_opens_degraded_review(
    tmp_path: Path,
    workflow_key: str,
) -> None:
    engine = _engine(tmp_path / workflow_key, workflow_key, RuntimeError("item-only"))

    action = engine.next_action(workflow_key)

    assert action["action"] == "review_required"
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    material = json.loads(Path(packet["material_file"]).read_text(encoding="utf-8"))
    assert packet["material_state"] == "degraded"
    assert packet["required_uncertain_items"] == [
        item["item_key"] for item in packet["items"]
    ]
    assert material["resources"] == []
    assert material["collection_gaps"] == [{"code": "material_collection_failed"}]


def test_degraded_review_closes_with_agent_generated_finance_explanation(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        "travel_expense",
        RuntimeError("resource_ownership_untrusted"),
    )
    action = engine.next_action("travel_expense")
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    decision = _uncertain_decision(action, packet)
    decision["items"][0]["conclusion"] = (
        "报销周期所需材料归属无法确认，请财务结合原始单据判断。"
    )
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, decision, indent=2)

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert closed["action"] == "closed"
    assert closed["business_state"] == "needs_human"
    assert closed["write_state"] == "not_requested"
    remark = Path(closed["remark_file"]).read_text(encoding="utf-8")
    assert remark.startswith("AI审核结论：需人工判断 ⚠️\n")
    assert "报销周期所需材料归属无法确认，请财务结合原始单据判断。" in remark


def test_degraded_review_rejects_a_false_pass(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "travel_expense", RuntimeError("timeout"))
    action = engine.next_action("travel_expense")
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    decision = _uncertain_decision(action, packet)
    decision["items"][0].update(
        status="pass",
        evidence_refs=["material:declared_fields.fake"],
        missing_evidence=[],
    )
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, decision, indent=2)

    with pytest.raises(ValueError, match="^review_decision_invalid$"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_packaging_collection_failure_remains_a_hard_stop(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        "packaging_layout_approval",
        RuntimeError("packaging_material_incomplete"),
    )

    action = engine.next_action("packaging_layout_approval")

    assert action["action"] == "needs_admin"
    assert action["reason"] == "material_collection_failed"


def test_explicit_runtime_stop_is_not_converted_to_a_material_gap(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, "travel_expense", RuntimeError("oa_run_stopped"))

    action = engine.next_action("travel_expense")

    assert action["action"] == "needs_admin"
    assert action["reason"] == "material_collection_failed"


def test_authorized_writer_can_submit_the_dynamic_gap_opinion(tmp_path: Path) -> None:
    submissions: list[dict] = []
    engine = _engine(
        tmp_path,
        "travel_expense",
        RuntimeError("resource_ownership_untrusted"),
        controlled=True,
        submissions=submissions,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    decision_file = tmp_path / "controlled-decision.json"
    json_artifact.write_json_atomic(
        decision_file,
        _uncertain_decision(action, packet),
        indent=2,
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert closed["write_state"] == "submitted"
    assert len(submissions) == 1
