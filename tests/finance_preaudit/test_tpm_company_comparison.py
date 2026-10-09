from __future__ import annotations

import io
import json
import hashlib
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import FlowEngine, codex_review_worker


def engine_for(tmp_path: Path, *, primary="甲公司", auxiliary="乙公司", buyer="乙公司", missing=(), primary_source="display", primary_status="present", submission_calls=None) -> FlowEngine:
    fields = [
        {"code": code, "name": name, "path": f"main[0].field[{index}]",
         "value": value, "value_source": "display", "read_status": "present"}
        for index, (code, name, value) in enumerate((
            ("zfgs", "支付公司", primary), ("zfgswb", "支付公司文本", primary),
            ("zfzhmc", "支付账户名称", primary), ("bxgs", "报销公司", primary),
            ("zfgs2", "支付公司2", auxiliary), ("kpgs", "开票公司", buyer),
        )) if code not in missing
    ]
    for row in fields:
        if row["code"] == "zfgs":
            row.update(value_source=primary_source, read_status=primary_status)

    def grant(context):
        return {"schema_version": 1, "kind": "preaudit_write_grant", "grant_id": "synthetic-company-grant",
                **context, "mode": "controlled_write", "expires_at": "2099-01-01T00:00:00+08:00", "proof": "synthetic-verified"}

    def submit(job):
        submission_calls.append(dict(job))
        return {"schema_version": 1, "kind": "preaudit_submission_result",
                "submission_token": job["submission_token"], "write": True, "submit_ok": True,
                "done_hit": True, "postcheck_done_hit": True}

    return FlowEngine(
        root=tmp_path / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda key: [{"request_id": "sanitized-tpm", "workflow_key": key,
                                          "received_at": "2026-10-01T09:00:00+08:00"}],
        collect_material=lambda request, attempt: {
            "schema_version": 1, "kind": "preaudit_material", "request_id": request,
            "workflow_key": "tpm_small_marketing_fee", "workflow_name": "TPM小额市场费",
            "declared_fields": fields, "resources": [], "warnings": [],
            "prepared": {"images": [], "structured_contents": [], "manifest": []},
            "authority_facts": [] if buyer is None else [{"source": "invoice_cloud",
                "name": "发票购买方", "value": buyer, "subject_ref": "sanitized-invoice"}],
        },
        authorization_source=grant if submission_calls is not None else None,
        authorization_verifier=(lambda supplied, context: supplied == grant(context)) if submission_calls is not None else None,
        submit_frozen=submit if submission_calls is not None else None,
    )


def decision_for(action: dict) -> dict:
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    return {"schema_version": 1, "kind": "preaudit_review_decision",
            "action_token": action["action_token"], "workflow_key": packet["workflow_key"],
            "rule_version": packet["rule_version"], "overall": "needs_human", "items": [
                {"item_key": item["item_key"], "status": "pass" if item["item_key"] == "entity_consistency" else "uncertain",
                 "conclusion": "使用乙公司（发票购买方）账户支付。" if item["item_key"] == "entity_consistency" else "当前项目待核。",
                 "evidence_refs": ["material:declared_fields.main[0].field[0]"] if item["item_key"] == "entity_consistency" else [],
                 "missing_evidence": [] if item["item_key"] == "entity_consistency" else ["材料待核"]}
                for item in packet["items"]]}


def test_recommendation_cannot_replace_original_company_comparison(tmp_path):
    engine = engine_for(tmp_path)
    action = engine.next_action("tpm_small_marketing_fee")
    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.validate_review_decision(Path(action["action_file"]), decision_for(action))


def test_complete_material_without_any_company_facts_cannot_pass(tmp_path):
    engine = engine_for(tmp_path, buyer=None, missing=("zfgs", "zfgswb", "zfzhmc", "bxgs", "zfgs2", "kpgs"))
    action = engine.next_action("tpm_small_marketing_fee")
    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.validate_review_decision(Path(action["action_file"]), decision_for(action))
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    assert packet["company_comparison_contract"] == {"version": 1, "records": []}
    decision = compared_decision(action, result="unknown", reason="实际支付公司、报销公司与权威购买方均未读取，请核对原单。")
    engine.validate_review_decision(Path(action["action_file"]), decision)


def compared_decision(action: dict, *, result="mismatch", reason="实际支付公司及报销公司甲公司，辅助支付公司2和开票公司乙公司，购买方乙公司，与实际主体不同。") -> dict:
    decision = decision_for(action)
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    decision["company_comparison"] = {
        "result": result, "reason": reason,
        "observed_refs": [row["evidence_ref"] for row in packet["company_comparison_contract"]["records"]],
    }
    item = decision["items"][-1]
    item["status"] = {"match": "pass", "mismatch": "fail", "unknown": "uncertain"}[result]
    item["conclusion"] = reason
    item["missing_evidence"] = ["实际主体或购买方待核"] if result == "unknown" else []
    return decision


def test_primary_and_auxiliary_roles_survive_preview_and_conflict_closes_normally(tmp_path):
    engine = engine_for(tmp_path)
    action = engine.next_action("tpm_small_marketing_fee")
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    records = packet["company_comparison_contract"]["records"]
    assert [(row["role"], row["value"]) for row in records] == [
        ("payment_company", "甲公司"), ("payment_company_text", "甲公司"),
        ("payment_account_name", "甲公司"), ("reimbursement_company", "甲公司"),
        ("auxiliary_payment_company", "乙公司"), ("invoice_company", "乙公司"),
        ("invoice_buyer", "乙公司"),
    ]
    decision = compared_decision(action)
    engine.validate_review_decision(Path(action["action_file"]), decision)
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(json.dumps(decision))
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    assert (closed["action"], closed["business_state"], closed["write_state"]) == (
        "closed", "needs_human", "not_requested")


def test_matching_companies_require_factual_conclusion(tmp_path):
    engine = engine_for(tmp_path, primary="乙公司")
    action = engine.next_action("tpm_small_marketing_fee")
    decision = compared_decision(action, result="match", reason="报销公司乙公司、支付公司乙公司、购买方乙公司一致；辅助支付公司2、开票公司和支付账户也为乙公司。")
    engine.validate_review_decision(Path(action["action_file"]), decision)
    decision["items"][-1]["conclusion"] = "使用乙公司（发票购买方）账户支付。"
    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.validate_review_decision(Path(action["action_file"]), decision)


@pytest.mark.parametrize("missing", ["primary", "buyer"])
def test_missing_company_or_buyer_is_specific_unknown_without_auxiliary_substitution(tmp_path, missing):
    engine = engine_for(tmp_path, buyer=None if missing == "buyer" else "乙公司",
                        missing=("zfgs",) if missing == "primary" else ())
    action = engine.next_action("tpm_small_marketing_fee")
    decision = compared_decision(action, result="unknown", reason="报销及支付账户甲公司，辅助支付公司2乙公司；实际支付公司或购买方未核实。")
    engine.validate_review_decision(Path(action["action_file"]), decision)
    decision["company_comparison"]["result"] = "match"
    decision["items"][-1].update(status="pass", missing_evidence=[])
    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.validate_review_decision(Path(action["action_file"]), decision)


def test_observation_cannot_omit_auxiliary_or_replace_primary_reference(tmp_path):
    engine = engine_for(tmp_path)
    action = engine.next_action("tpm_small_marketing_fee")
    decision = compared_decision(action)
    decision["company_comparison"]["observed_refs"].pop(0)
    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.validate_review_decision(Path(action["action_file"]), decision)


def test_observation_reason_cannot_hide_conflicting_original_value(tmp_path):
    engine = engine_for(tmp_path)
    action = engine.next_action("tpm_small_marketing_fee")
    decision = compared_decision(action)
    decision["company_comparison"]["reason"] = "购买方与辅助支付公司2乙公司相同，使用乙公司账户支付。"
    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.validate_review_decision(Path(action["action_file"]), decision)


def test_new_packet_cannot_drop_company_contract_or_disguise_as_legacy(tmp_path):
    engine = engine_for(tmp_path)
    action = engine.next_action("tpm_small_marketing_fee")
    packet_file = Path(action["review_packet_file"])
    packet = json.loads(packet_file.read_text())
    packet.pop("company_comparison_contract")
    packet_file.write_text(json.dumps(packet))
    with pytest.raises(ValueError, match="frozen_packet_changed"):
        engine.validate_review_decision(Path(action["action_file"]), decision_for(action))
    state_file = Path(action["state_file"])
    state = json.loads(state_file.read_text())
    state.pop("company_comparison_contract_version")
    state_file.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="frozen_packet_changed"):
        engine.validate_review_decision(Path(action["action_file"]), decision_for(action))


def test_actual_legacy_marker_free_frozen_packet_keeps_original_decision_contract(tmp_path):
    engine = engine_for(tmp_path)
    action = engine.next_action("tpm_small_marketing_fee")
    # Construct a prior-release frozen preview fixture with its original fingerprints.
    material_file = Path(action["action_file"]).with_name("material.json")
    material = json.loads(material_file.read_text())
    material.pop("company_comparison_contract")
    material_file.write_text(json.dumps(material))
    fingerprint = "sha256:" + hashlib.sha256(material_file.read_bytes()).hexdigest()
    packet_file = Path(action["review_packet_file"])
    packet = json.loads(packet_file.read_text())
    packet.pop("company_comparison_contract")
    packet["material_fingerprint"] = fingerprint
    packet_file.write_text(json.dumps(packet))
    state_file = Path(action["state_file"])
    state = json.loads(state_file.read_text())
    state.pop("company_comparison_contract_version")
    state["material_fingerprint"] = fingerprint
    state_file.write_text(json.dumps(state))
    engine.validate_review_decision(Path(action["action_file"]), decision_for(action))
    decision_file = tmp_path / "legacy-decision.json"
    decision_file.write_text(json.dumps(decision_for(action)))
    engine.complete_review(Path(action["action_file"]), decision_file)
    assert "company_comparison_contract_version" not in json.loads(state_file.read_text())


def test_marker_role_or_original_value_tampering_rejected(tmp_path):
    engine = engine_for(tmp_path)
    action = engine.next_action("tpm_small_marketing_fee")
    decision = compared_decision(action)
    packet_file = Path(action["review_packet_file"])
    packet = json.loads(packet_file.read_text())
    packet["company_comparison_contract"]["records"][0]["role"] = "auxiliary_payment_company"
    packet_file.write_text(json.dumps(packet))
    with pytest.raises(ValueError, match="frozen_packet_changed"):
        engine.validate_review_decision(Path(action["action_file"]), decision)


def test_actual_worker_uses_bound_company_schema_and_preserves_model_comparison(tmp_path, monkeypatch):
    from jsonschema import validate

    engine = engine_for(tmp_path)
    action = engine.next_action("tpm_small_marketing_fee")
    decision = compared_decision(action)
    captured = {}

    class Process:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            self.stderr = io.StringIO()
            self.destination = Path(command[command.index("--output-last-message") + 1])
            captured["schema"] = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())

        def communicate(self, *, input, timeout):
            captured["prompt"] = input
            self.destination.write_text(json.dumps(decision))

        def poll(self):
            return self.returncode

    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", Process)
    result = codex_review_worker.review({"project_root": str(tmp_path),
                                         "action_file": action["action_file"], "timeout_seconds": 60})
    assert result["status"] == "reviewed"
    assert "company_comparison" in captured["schema"]["required"]
    validate(decision, captured["schema"])
    assert "zfgs2 仅是辅助支付公司2" in captured["prompt"]
    assert "不得仅给账户支付建议而判一致" in captured["prompt"]
    assert result["decision"]["company_comparison"] == decision["company_comparison"]
    engine.validate_review_decision(Path(action["action_file"]), result["decision"])
    assert json.loads(Path(action["action_file"]).read_text())["action"] == "review_required"


def test_bracket_values_stay_original_while_model_can_apply_allowed_normalization(tmp_path):
    engine = engine_for(tmp_path, primary=" 乙公司（华东） ", auxiliary="乙公司(华东)", buyer="乙公司(华东)")
    action = engine.next_action("tpm_small_marketing_fee")
    reason = "报销公司 乙公司（华东） 、支付公司 乙公司（华东） 、购买方乙公司(华东)经括号与首尾空白统一后一致；辅助及开票公司乙公司(华东)。"
    decision = compared_decision(action, result="match", reason=reason)
    engine.validate_review_decision(Path(action["action_file"]), decision)


@pytest.mark.parametrize("source", ["raw", "display"])
def test_original_value_source_is_preserved_and_unparsed_primary_is_unknown(tmp_path, source):
    engine = engine_for(tmp_path, primary_source=source, primary_status="unresolved")
    action = engine.next_action("tpm_small_marketing_fee")
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    primary = packet["company_comparison_contract"]["records"][0]
    assert (primary["value_source"], primary["read_status"], primary["value"]) == (source, "unresolved", "甲公司")
    decision = compared_decision(action, result="unknown", reason="支付公司未解析；支付文本、账户及报销公司甲公司，辅助及开票公司、购买方乙公司，实际支付主体需核对。")
    engine.validate_review_decision(Path(action["action_file"]), decision)
    decision_file = tmp_path / "unparsed-decision.json"
    decision_file.write_text(json.dumps(decision))
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    assert (closed["business_state"], closed["write_state"]) == ("needs_human", "not_requested")


def test_observation_result_and_item_cannot_disagree(tmp_path):
    engine = engine_for(tmp_path)
    action = engine.next_action("tpm_small_marketing_fee")
    decision = compared_decision(action)
    decision["items"][-1]["status"] = "pass"
    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.validate_review_decision(Path(action["action_file"]), decision)


def test_mock_submission_retains_company_binding_and_confirmation_is_read_only(tmp_path):
    calls = []
    engine = engine_for(tmp_path, submission_calls=calls)
    action = engine.next_action("tpm_small_marketing_fee")
    decision_file = tmp_path / "controlled-decision.json"
    decision_file.write_text(json.dumps(compared_decision(action)))
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    assert closed["write_state"] == "submitted"
    state = json.loads(Path(action["state_file"]).read_text())
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in Path(action["action_file"]).parent.iterdir() if path.is_file()}
    assert FlowEngine.confirmed_submission(tmp_path / "runtime" / "finance_preaudit_flows", state)
    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in Path(action["action_file"]).parent.iterdir() if path.is_file()}
    assert before == after
    assert state["company_comparison_contract_version"] == 1
    assert len(calls) == 1
    state.pop("company_comparison_contract_version")
    state_file = Path(action["state_file"])
    state_file.write_text(json.dumps(state))
    original_bytes = state_file.read_bytes()
    assert not FlowEngine.confirmed_submission(tmp_path / "runtime" / "finance_preaudit_flows", state)
    assert state_file.read_bytes() == original_bytes


def test_receipt_recovery_retains_company_binding_without_repeating_mock_writer(tmp_path, monkeypatch):
    from apps import json_artifact

    calls = []
    engine = engine_for(tmp_path, submission_calls=calls)
    action = engine.next_action("tpm_small_marketing_fee")
    decision_file = tmp_path / "controlled-decision.json"
    decision_file.write_text(json.dumps(compared_decision(action)))
    original_write = json_artifact.write_json_atomic
    tripped = False

    def crash_after_receipt(path, payload, *args, **kwargs):
        nonlocal tripped
        original_write(path, payload, *args, **kwargs)
        if Path(path).name == "execution-receipt.json" and not tripped:
            tripped = True
            raise SystemExit("receipt-persisted")

    monkeypatch.setattr(json_artifact, "write_json_atomic", crash_after_receipt)
    with pytest.raises(SystemExit, match="receipt-persisted"):
        engine.complete_review(Path(action["action_file"]), decision_file)
    recovered = engine.next_action("tpm_small_marketing_fee")
    assert recovered["write_state"] == "submitted"
    state = json.loads(Path(action["state_file"]).read_text())
    assert FlowEngine.confirmed_submission(tmp_path / "runtime" / "finance_preaudit_flows", state)
    assert state["company_comparison_contract_version"] == 1
    assert len(calls) == 1
