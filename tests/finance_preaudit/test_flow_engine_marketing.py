from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import cli, flow_engine, flow_evals


WORKFLOW_KEY = "small_marketing_fee"
ITEM_KEYS = (
    "budget_account",
    "attachment_completeness",
    "cost_bearer",
    "sap_customer_code_match",
    "reimbursable_minimum",
    "payment_company_consistency",
)
ITEM_NAMES = (
    "预算科目",
    "附件完整性",
    "费用承担主体",
    "SAP 客户编码匹配",
    "金额孰低核销",
    "支付公司一致",
)
FACT_KEYS = (
    "budget_account",
    "attachment",
    "cost_bearer",
    None,
    "amount",
    "payment_company",
)
FIXTURE_ROOT = Path(__file__).parent / "fixtures"
REPLAY = json.loads(
    (FIXTURE_ROOT / "small_marketing_reviewer_replay.json").read_text(
        encoding="utf-8"
    )
)


def _material(request_id: str, workflow_key: str, facts: dict | list) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_material",
        "request_id": request_id,
        "workflow_key": workflow_key,
        "workflow_name": "小额市场费报销流程",
        "declared_fields": facts.copy(),
        "resources": [],
        "prepared": {"images": [], "structured_contents": [], "manifest": []},
        "authority_facts": [],
        "warnings": [],
    }


def _grant(context: dict) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_write_grant",
        "grant_id": "marketing-test-grant",
        **context,
        "mode": "controlled_write",
        "expires_at": "2026-08-22T12:00:00+08:00",
        "proof": "verified-by-marketing-test",
    }


def _engine(
    root: Path,
    *,
    material_workflow: str = WORKFLOW_KEY,
    facts: dict | list | None = None,
    calls: list[dict] | None = None,
) -> flow_engine.FlowEngine:
    current_facts = facts if facts is not None else {
        key: "matched" for key in FACT_KEYS if key is not None
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

    controlled = calls is not None
    return flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [{
            "request_id": "marketing-request",
            "received_at": "2026-08-22T09:00:00+08:00",
            "workflow_key": workflow_key,
        }],
        collect_material=lambda request_id, _attempt_root: _material(
            request_id, material_workflow, current_facts
        ),
        authorization_source=_grant if controlled else None,
        authorization_verifier=(
            (lambda grant, context: grant == _grant(context)) if controlled else None
        ),
        submit_frozen=submit if controlled else None,
    )


def _reviewed_items(statuses: list[str]) -> list[dict]:
    reviewed = []
    for item_key, fact_key, status in zip(
        ITEM_KEYS,
        FACT_KEYS,
        statuses,
        strict=True,
    ):
        if item_key == "sap_customer_code_match":
            conclusion = "SAP 主数据接口未接入，无法核验。"
        elif item_key == "attachment_completeness":
            conclusion = (
                "支付截图和发票均完整。"
                if status == "pass"
                else "支付截图或发票不完整。"
                if status == "fail"
                else "支付截图和发票的完整性证据不足。"
            )
        elif item_key == "payment_company_consistency":
            conclusion = (
                "报销公司、支付公司、购买方三者一致，应选择示例有限公司支付。"
                if status == "pass"
                else "报销公司、支付公司与购买方不一致。"
                if status == "fail"
                else "报销公司、支付公司或购买方证据不足。"
            )
        else:
            conclusion = "当前审核项已按脱敏冻结事实判断。"
        reviewed.append(
            {
                "status": status,
                "conclusion": conclusion,
                "evidence_refs": (
                    []
                    if status == "uncertain"
                    else [f"material:declared_fields.{fact_key}"]
                ),
                "missing_evidence": (
                    ["SAP 主数据"]
                    if fact_key is None
                    else ["当前审核项证据不足"]
                    if status == "uncertain"
                    else []
                ),
            }
        )
    return reviewed


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


def test_marketing_preview_cli_and_items_are_isolated(tmp_path: Path, capsys) -> None:
    engine = _engine(tmp_path)
    assert cli.main(
        ["next-action", "--workflow", WORKFLOW_KEY],
        flow_engine_factory=lambda: engine,
    ) == 0
    action = json.loads(capsys.readouterr().out)
    packet = json.loads(Path(action["review_packet_file"]).read_text(encoding="utf-8"))
    assert action["coordination_seconds"] == action["coordination_tokens"] == 0
    state = json.loads(Path(action["state_file"]).read_text(encoding="utf-8"))
    assert action["rule_version"] == packet["rule_version"] == state["rule_version"]
    assert packet["rule_version"] == "small_marketing_fee:2"
    assert tuple(item["item_key"] for item in packet["items"]) == ITEM_KEYS
    assert tuple(item["name"] for item in packet["items"]) == ITEM_NAMES
    assert "业务事项合规性" not in str(packet)

    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(
            action,
            _reviewed_items(
                ["pass", "pass", "pass", "uncertain", "pass", "pass"]
            ),
        ),
    )
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    assert (closed["business_state"], closed["write_state"]) == (
        "needs_human", "not_requested"
    )


def test_marketing_legacy_v1_packet_can_finish_original_five_items(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    action_file = Path(action["action_file"])
    state_file = Path(action["state_file"])
    packet_file = Path(action["review_packet_file"])
    legacy_action = json.loads(action_file.read_text(encoding="utf-8"))
    legacy_state = json.loads(state_file.read_text(encoding="utf-8"))
    legacy_packet = json.loads(packet_file.read_text(encoding="utf-8"))
    legacy_action.pop("rule_version")
    legacy_state.pop("rule_version")
    legacy_packet["rule_version"] = "small_marketing_fee:1"
    legacy_packet["items"] = legacy_packet["items"][:-1]
    legacy_packet["required_uncertain_items"] = [
        item_key
        for item_key in legacy_packet["required_uncertain_items"]
        if item_key != "payment_company_consistency"
    ]
    json_artifact.write_json_atomic(action_file, legacy_action)
    json_artifact.write_json_atomic(state_file, legacy_state)
    json_artifact.write_json_atomic(packet_file, legacy_packet)

    decision_file = tmp_path / "legacy-v1.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(
            legacy_action,
            _reviewed_items(
                ["pass", "pass", "pass", "uncertain", "pass", "pass"]
            )[:-1],
        ),
    )

    closed = engine.complete_review(action_file, decision_file)

    assert closed["business_state"] == "needs_human"


def test_marketing_v2_requires_payment_and_invoice_in_fallback_conclusion(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        facts={
            "budget_account": "市场物料费",
            "attachment": "complete",
            "cost_bearer": "matched",
            "amount": "within_limit",
            "payment_company": "matched",
        },
    )
    action = engine.next_action(WORKFLOW_KEY)
    reviewed = _reviewed_items(
        ["pass", "pass", "pass", "uncertain", "pass", "pass"]
    )
    reviewed[1]["conclusion"] = "附件完整。"
    decision_file = tmp_path / "missing-fallback-terms.json"
    json_artifact.write_json_atomic(decision_file, _decision(action, reviewed))

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_marketing_real_field_rows_require_fallback_terms(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        facts=[
            {
                "path": "detail[1].row[1].budget",
                "name": "预算科目",
                "code": "budget",
                "value": "市场物料费",
            }
        ],
    )
    action = engine.next_action(WORKFLOW_KEY)
    reviewed = _reviewed_items(
        ["pass", "pass", "pass", "uncertain", "pass", "pass"]
    )
    reviewed[1]["conclusion"] = "附件完整。"
    decision_file = tmp_path / "real-fields-missing-fallback-terms.json"
    json_artifact.write_json_atomic(decision_file, _decision(action, reviewed))

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_marketing_special_subject_keeps_existing_attachment_conclusion(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        facts={
            "budget_account": "配送费-自提/租车费",
            "attachment": "complete",
            "cost_bearer": "matched",
            "amount": "within_limit",
            "payment_company": "matched",
        },
    )
    action = engine.next_action(WORKFLOW_KEY)
    reviewed = _reviewed_items(
        ["pass", "pass", "pass", "uncertain", "pass", "pass"]
    )
    reviewed[1]["conclusion"] = "附件已按现有科目规则核对。"
    decision_file = tmp_path / "special-subject.json"
    json_artifact.write_json_atomic(decision_file, _decision(action, reviewed))

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert closed["business_state"] == "needs_human"


def test_marketing_company_pass_requires_prescribed_conclusion(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    reviewed = _reviewed_items(
        ["pass", "pass", "pass", "uncertain", "pass", "pass"]
    )
    reviewed[-1]["conclusion"] = "报销公司、支付公司与购买方一致。"
    decision_file = tmp_path / "invalid-company-pass.json"
    json_artifact.write_json_atomic(decision_file, _decision(action, reviewed))

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_marketing_controlled_substitute_is_bound_and_idempotent(tmp_path: Path) -> None:
    calls: list[dict] = []
    engine = _engine(tmp_path, calls=calls)
    action = engine.next_action(WORKFLOW_KEY)
    decision_file = tmp_path / "controlled.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(
            action,
            _reviewed_items(
                ["pass", "pass", "pass", "uncertain", "pass", "pass"]
            ),
        ),
    )
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    replayed = engine.complete_review(Path(action["action_file"]), decision_file)
    receipt = json.loads(Path(closed["receipt_file"]).read_text(encoding="utf-8"))
    assert closed == replayed
    assert len(calls) == 1
    assert calls[0]["workflow_key"] == receipt["workflow_key"] == WORKFLOW_KEY


def test_marketing_independent_reviewer_replay_scores_100_percent(tmp_path: Path) -> None:
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
                "outcome": "technical_failure", "items": [], "stop_code": action["reason"]
            }
            continue
        decision_file = tmp_path / f"{case['case_id']}.json"
        json_artifact.write_json_atomic(decision_file, _decision(action, case["items"]))
        closed = engine.complete_review(Path(action["action_file"]), decision_file)
        packet = json.loads(Path(action["review_packet_file"]).read_text(encoding="utf-8"))
        material = json.loads(Path(packet["material_file"]).read_text(encoding="utf-8"))
        statuses = [marks[item["status"]] for item in case["items"]]
        decision = json.loads(decision_file.read_text(encoding="utf-8"))
        for item in decision["items"]:
            for ref in item["evidence_refs"]:
                assert ref.removeprefix("material:declared_fields.") in material["declared_fields"]
        actuals[case["case_id"]] = {
            "outcome": (
                "business_warning" if "❌" in statuses
                else "evidence_insufficient" if "⚠️" in statuses else "pass"
            ),
            "items": [
                {"name": item["name"], "status": status}
                for item, status in zip(packet["items"], statuses, strict=True)
            ],
            "stop_code": None,
        }
        assert json.loads(Path(closed["receipt_file"]).read_text(encoding="utf-8"))[
            "workflow_key"
        ] == WORKFLOW_KEY
    report = flow_evals.run_replay(suite, actuals)
    assert report["accuracy"] == report["format_compliance"] == 1.0
    assert not [item for item in report["differences"] if item["workflow_key"] == WORKFLOW_KEY]
