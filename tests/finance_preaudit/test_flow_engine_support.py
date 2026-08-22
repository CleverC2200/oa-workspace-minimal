from __future__ import annotations

import json
from pathlib import Path

from apps import json_artifact
from apps.oa_finance_preaudit import cli, flow_engine, flow_evals


WORKFLOW_KEY = "business_support_fee"
ITEM_NAMES = ("附件完整性", "业务事项合规性", "预算校验", "金额孰低核销")
ITEM_KEYS = (
    "attachment_completeness",
    "business_compliance",
    "budget_check",
    "reimbursable_minimum",
)
FIXTURE_ROOT = Path(__file__).parent / "fixtures"
REPLAY = json.loads(
    (FIXTURE_ROOT / "business_support_reviewer_replay.json").read_text(
        encoding="utf-8"
    )
)


def _material(
    request_id: str,
    workflow_key: str = WORKFLOW_KEY,
    facts: dict | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_material",
        "request_id": request_id,
        "workflow_key": workflow_key,
        "workflow_name": "业务支持费报销流程",
        "declared_fields": dict(facts or {}),
        "resources": [],
        "prepared": {"images": [], "structured_contents": [], "manifest": []},
        "authority_facts": [],
        "warnings": [],
    }


def _grant(context: dict) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_write_grant",
        "grant_id": "support-test-grant",
        **context,
        "mode": "controlled_write",
        "expires_at": "2026-08-22T12:00:00+08:00",
        "proof": "verified-by-support-test",
    }


def _engine(
    root: Path,
    *,
    material_workflow: str = WORKFLOW_KEY,
    controlled: bool = False,
    calls: list[dict] | None = None,
    facts: dict | None = None,
) -> flow_engine.FlowEngine:
    material_facts = facts if facts is not None else {
        "attachment": "complete",
        "business": "supported",
        "amount": "within_limit",
    }

    def submit(job: dict) -> dict:
        if calls is not None:
            calls.append(dict(job))
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
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "support-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: _material(
            request_id, material_workflow, material_facts
        ),
        authorization_source=_grant if controlled else None,
        authorization_verifier=(
            (lambda grant, context: grant == _grant(context)) if controlled else None
        ),
        submit_frozen=submit if controlled else None,
    )


def _reviewed_items(statuses: list[str]) -> list[dict]:
    fact_keys = ("attachment", "business", None, "amount")
    return [
        {
            "status": status,
            "conclusion": (
                "当前阶段按既定策略暂不校验，OA 暂未提供预算数据。"
                if key is None
                else "当前审核项已按冻结证据判断。"
            ),
            "evidence_refs": (
                []
                if status == "uncertain"
                else [
                    "rule:business-support-budget-not-checked"
                    if key is None
                    else f"material:declared_fields.{key}"
                ]
            ),
            "missing_evidence": ["当前审核项证据不足"] if status == "uncertain" else [],
        }
        for key, status in zip(fact_keys, statuses, strict=True)
    ]


def _decision(action: dict, reviewed_items: list[dict]) -> dict:
    packet = json.loads(Path(action["review_packet_file"]).read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "action_token": action["action_token"],
        "workflow_key": WORKFLOW_KEY,
        "rule_version": packet["rule_version"],
        "overall": (
            "pass"
            if all(item["status"] == "pass" for item in reviewed_items)
            else "needs_human"
        ),
        "items": [
            {
                "item_key": item["item_key"],
                **reviewed,
            }
            for item, reviewed in zip(packet["items"], reviewed_items, strict=True)
        ],
    }


def test_support_preview_and_cli_use_isolated_four_item_contract(
    tmp_path: Path,
    capsys,
) -> None:
    engine = _engine(tmp_path)
    assert cli.main(
        ["next-action", "--workflow", WORKFLOW_KEY],
        flow_engine_factory=lambda: engine,
    ) == 0
    action = json.loads(capsys.readouterr().out)
    packet = json.loads(Path(action["review_packet_file"]).read_text(encoding="utf-8"))
    assert action["coordination_seconds"] == action["coordination_tokens"] == 0
    assert packet["rule_version"] == "business_support_fee:1"
    assert tuple(item["item_key"] for item in packet["items"]) == ITEM_KEYS
    assert tuple(item["name"] for item in packet["items"]) == ITEM_NAMES
    assert "附件完整性/支付证据" not in str(packet)

    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, _reviewed_items(["pass"] * 4)),
    )
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    assert (closed["business_state"], closed["write_state"]) == (
        "pass",
        "not_requested",
    )


def test_support_controlled_substitute_is_bound_and_idempotent(tmp_path: Path) -> None:
    calls: list[dict] = []
    engine = _engine(tmp_path, controlled=True, calls=calls)
    action = engine.next_action(WORKFLOW_KEY)
    decision_file = tmp_path / "controlled.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, _reviewed_items(["pass"] * 4)),
    )
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    replayed = engine.complete_review(Path(action["action_file"]), decision_file)
    receipt = json.loads(Path(closed["receipt_file"]).read_text(encoding="utf-8"))
    assert closed == replayed
    assert len(calls) == 1
    assert calls[0]["workflow_key"] == receipt["workflow_key"] == WORKFLOW_KEY
    assert closed["write_state"] == "submitted"


def test_support_independent_reviewer_replay_scores_100_percent(tmp_path: Path) -> None:
    suite = flow_evals.load_suite(FIXTURE_ROOT / "golden_replay.json")
    actuals = flow_evals.expected_actuals(suite)
    marks = {"pass": "✅", "fail": "❌", "uncertain": "⚠️"}

    for case in REPLAY["cases"]:
        engine = _engine(
            tmp_path / case["case_id"],
            material_workflow=case["material_workflow"],
            facts=case["facts"],
        )
        action = engine.next_action(WORKFLOW_KEY)
        if action["action"] == "needs_admin":
            actuals[case["case_id"]] = {
                "outcome": "technical_failure",
                "items": [],
                "stop_code": action["reason"],
            }
            continue
        decision_file = tmp_path / f"{case['case_id']}.json"
        json_artifact.write_json_atomic(
            decision_file, _decision(action, case["items"])
        )
        closed = engine.complete_review(Path(action["action_file"]), decision_file)
        packet = json.loads(
            Path(action["review_packet_file"]).read_text(encoding="utf-8")
        )
        material = json.loads(
            Path(packet["material_file"]).read_text(encoding="utf-8")
        )
        for reviewed in case["items"]:
            for evidence_ref in reviewed["evidence_refs"]:
                if evidence_ref.startswith("material:declared_fields."):
                    fact_key = evidence_ref.removeprefix("material:declared_fields.")
                    assert fact_key in material["declared_fields"]
                else:
                    assert evidence_ref == "rule:business-support-budget-not-checked"
        assert case["items"][2]["conclusion"] == (
            "当前阶段按既定策略暂不校验，OA 暂未提供预算数据。"
        )
        statuses = [marks[item["status"]] for item in case["items"]]
        actuals[case["case_id"]] = {
            "outcome": (
                "evidence_insufficient"
                if "⚠️" in statuses
                else "business_warning" if "❌" in statuses else "pass"
            ),
            "items": [
                {"name": item["name"], "status": status}
                for item, status in zip(packet["items"], statuses, strict=True)
            ],
            "stop_code": None,
        }
        assert json.loads(
            Path(closed["receipt_file"]).read_text(encoding="utf-8")
        )["workflow_key"] == WORKFLOW_KEY

    report = flow_evals.run_replay(suite, actuals)
    assert report["accuracy"] == report["format_compliance"] == 1.0
    assert not [
        item
        for item in report["differences"]
        if item["workflow_key"] == WORKFLOW_KEY
    ]
