"""OA invoice relationships remain separate from uploaded invoice contents."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import codex_review_worker, flow_engine


def _review(
    tmp_path: Path,
    workflow_key: str,
    rows: list[dict],
    *,
    facts: list[dict] | None = None,
):
    rows = list(rows)
    facts = list(facts or [])
    if workflow_key == "tpm_small_marketing_fee":
        buyer = next((fact["value"] for fact in facts if fact.get("source") == "invoice_cloud" and fact.get("name") == "发票购买方"), "测试公司")
        rows.extend({"path": f"main[0].field[{index}]", "code": code, "name": name,
                     "value": buyer, "read_status": "present", "value_source": "display"}
                    for index, (code, name) in enumerate((("zfgs", "支付公司"), ("bxgs", "报销公司"))))
        if not any(fact.get("source") == "invoice_cloud" and fact.get("name") == "发票购买方" for fact in facts):
            facts.append({"source": "invoice_cloud", "name": "发票购买方", "value": buyer, "subject_ref": "synthetic-invoice"})
    def collect(request_id: str, attempt_root: Path) -> dict:
        invoice = attempt_root / "attachments" / "invoice.pdf"
        invoice.write_bytes(b"synthetic-uploaded-invoice")
        return {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": request_id,
            "workflow_key": workflow_key,
            "declared_fields": rows,
            "resources": [
                {
                    "request_id": request_id,
                    "resource_id": "uploaded-invoice",
                    "source_system": "oa",
                    "source_resource_id": "uploaded-invoice",
                    "local_readonly_path": str(invoice),
                }
            ],
            "prepared": {
                "images": [],
                "structured_contents": [],
                "manifest": [
                    {"resource_id": "uploaded-invoice", "status": "unsupported_file"}
                ],
            },
            "authority_facts": list(facts or []),
            "warnings": [],
        }

    engine = flow_engine.FlowEngine(
        root=tmp_path / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda key: [
            {
                "request_id": "synthetic-case",
                "workflow_key": key,
                "received_at": "2026-08-22T09:00:00+08:00",
            }
        ],
        collect_material=collect,
    )
    action = engine.next_action(workflow_key, mode="preview")
    assert action["action"] == "review_required"
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    decision = {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "action_token": action["action_token"],
        "workflow_key": workflow_key,
        "rule_version": packet["rule_version"],
        "overall": "pass",
        "items": [
            {
                "item_key": item["item_key"],
                "status": "pass",
                "conclusion": "已核对当前审核项。",
                "evidence_refs": ["material:resources.uploaded-invoice"],
                "missing_evidence": [],
            }
            for item in packet["items"]
        ],
    }
    for item in decision["items"]:
        if item["item_key"] == "budget" and workflow_key == "general_expense":
            item["conclusion"] = "当前阶段按既定策略暂不校验部门年度预算。"
        if item["item_key"] == "payment_company_consistency":
            item["conclusion"] = (
                "报销公司、支付公司、购买方三者一致，应选择示例公司支付。"
            )
    if "company_comparison_contract" in packet:
        reason = f"报销公司{buyer}、支付公司{buyer}、购买方{buyer}一致。"
        decision["company_comparison"] = {
            "observed_refs": [row["evidence_ref"] for row in packet["company_comparison_contract"]["records"]],
            "result": "match", "reason": reason,
        }
        decision["items"][-1]["conclusion"] = reason
    return engine, action, decision


def _linked_field(status: str, value: str = "", row: int = 0) -> dict:
    return {
        "path": f"detail[0].row[{row}].field[17]",
        "name": "关联发票",
        "code": "glfp",
        "value": value,
        "read_status": status,
    }


def test_uploaded_invoice_cannot_turn_unresolved_oa_link_into_pass(
    tmp_path: Path,
) -> None:
    engine, action, decision = _review(
        tmp_path, "business_entertainment", [_linked_field("unresolved")]
    )
    engine.validate_review_decision(Path(action["action_file"]), decision)
    decision_file = tmp_path / "candidate.json"
    json_artifact.write_json_atomic(decision_file, decision)
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    assert closed["business_state"] == "needs_human"
    assert closed["write_state"] == "not_requested"
    remark = Path(closed["remark_file"]).read_text()
    assert "⚠️ 2、发票关联：关联发票未读取到关联记录，请财务核对原单。" in remark
    assert (
        decision["overall"] == "pass"
    )  # Consumer normalization never rewrites caller input.


@pytest.mark.parametrize(
    "workflow_key",
    ["business_entertainment", "general_expense", "tpm_small_marketing_fee"],
)
def test_actual_worker_prompt_distinguishes_oa_link_from_upload(
    tmp_path: Path, monkeypatch, workflow_key: str
) -> None:
    _engine, action, decision = _review(
        tmp_path, workflow_key, [_linked_field("unresolved")]
    )
    prompts = []
    monkeypatch.setenv("OA_REVIEW_API_KEY", "unit-test-key")

    class Process:
        pid = 12345
        returncode = 0
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")

        def communicate(self, *, input, timeout):
            prompts.append(input)

        def poll(self):
            return self.returncode

    def popen(command, **kwargs):
        Path(command[command.index("--output-last-message") + 1]).write_text(
            json.dumps(decision)
        )
        return Process()

    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", popen)
    result = codex_review_worker.review(
        {
            "project_root": str(tmp_path),
            "action_file": action["action_file"],
            "timeout_seconds": 60,
        }
    )
    assert result["status"] == "reviewed", result
    assert "上传的发票附件及其内容不证明 OA 已关联" in prompts[0]
    assert "不要求拆分明细逐行重复挂票" in prompts[0]
    assert "不能声称申请人未关联" in prompts[0]


@pytest.mark.parametrize(
    "workflow_key",
    ["business_entertainment", "general_expense", "tpm_small_marketing_fee"],
)
@pytest.mark.parametrize("read_status", ["unresolved", "empty"])
def test_missing_oa_link_closes_as_business_uncertainty(
    tmp_path: Path, workflow_key: str, read_status: str
) -> None:
    engine, action, decision = _review(
        tmp_path, workflow_key, [_linked_field(read_status)]
    )
    path = tmp_path / "candidate.json"
    json_artifact.write_json_atomic(path, decision)
    closed = engine.complete_review(Path(action["action_file"]), path)
    assert closed["execution_state"] == "closed"
    assert closed["business_state"] == "needs_human"
    assert (
        "关联发票未读取到关联记录，请财务核对原单。"
        in Path(closed["remark_file"]).read_text()
    )


@pytest.mark.parametrize(
    "workflow_key",
    ["business_entertainment", "general_expense", "tpm_small_marketing_fee"],
)
def test_trusted_oa_link_covers_split_rows_without_repeated_link(
    tmp_path: Path, workflow_key: str
) -> None:
    rows = [
        _linked_field("present", "12345678901234567890"),
        _linked_field("empty", row=1),
    ]
    engine, action, decision = _review(tmp_path, workflow_key, rows)
    linked_key = (
        "linked_invoice_receipt"
        if workflow_key == "tpm_small_marketing_fee"
        else "invoice_linkage"
    )
    item = next(item for item in decision["items"] if item["item_key"] == linked_key)
    item["evidence_refs"] = ["material:declared_fields.detail[0].row[0].field[17]"]
    engine.validate_review_decision(Path(action["action_file"]), decision)
    path = tmp_path / "candidate.json"
    json_artifact.write_json_atomic(path, decision)
    closed = engine.complete_review(Path(action["action_file"]), path)
    assert closed["business_state"] == "pass"
    assert closed["write_state"] == "not_requested"


def test_model_confirmed_missing_link_is_preserved_as_business_failure(
    tmp_path: Path,
) -> None:
    engine, action, decision = _review(
        tmp_path, "general_expense", [_linked_field("empty")]
    )
    item = next(
        item for item in decision["items"] if item["item_key"] == "invoice_linkage"
    )
    item.update(
        status="fail",
        conclusion="原单已确认未建立 OA 发票关联。",
        evidence_refs=["material:declared_fields.detail[0].row[0].field[17]"],
    )
    decision["overall"] = "needs_human"
    path = tmp_path / "candidate.json"
    json_artifact.write_json_atomic(path, decision)
    closed = engine.complete_review(Path(action["action_file"]), path)
    assert closed["business_state"] == "needs_human"
    assert (
        "❌ 2、发票关联：原单已确认未建立 OA 发票关联。"
        in Path(closed["remark_file"]).read_text()
    )


def test_legacy_material_without_link_read_status_keeps_original_contract(
    tmp_path: Path,
) -> None:
    row = _linked_field("present", "12345678901234567890")
    del row["read_status"]
    engine, action, decision = _review(tmp_path, "business_entertainment", [row])
    # Missing modern metadata is disclosed by the model instead of being inferred as an omission.
    item = next(
        item for item in decision["items"] if item["item_key"] == "invoice_linkage"
    )
    item.update(
        status="uncertain",
        conclusion="旧材料未保留关联字段读取状态，请财务核对原单。",
        missing_evidence=["可信 OA 关联记录"],
    )
    decision["overall"] = "needs_human"
    path = tmp_path / "candidate.json"
    json_artifact.write_json_atomic(path, decision)
    closed = engine.complete_review(Path(action["action_file"]), path)
    assert closed["execution_state"] == "closed"
    assert (
        "旧材料未保留关联字段读取状态，请财务核对原单。"
        in Path(closed["remark_file"]).read_text()
    )


@pytest.mark.parametrize(
    "workflow_key",
    ["business_entertainment", "general_expense", "tpm_small_marketing_fee"],
)
def test_new_material_without_oa_link_field_cannot_pass_from_upload(
    tmp_path: Path,
    workflow_key: str,
) -> None:
    engine, action, decision = _review(tmp_path, workflow_key, [])
    path = tmp_path / "candidate.json"
    json_artifact.write_json_atomic(path, decision)
    closed = engine.complete_review(Path(action["action_file"]), path)
    assert closed["business_state"] == "needs_human"
    assert (
        "关联发票未读取到关联记录，请财务核对原单。"
        in Path(closed["remark_file"]).read_text()
    )


@pytest.mark.parametrize(
    "workflow_key",
    ["business_entertainment", "general_expense", "tpm_small_marketing_fee"],
)
def test_packet_contract_cannot_be_deleted_to_bypass_new_link_guard(
    tmp_path: Path, workflow_key: str
) -> None:
    engine, action, decision = _review(tmp_path, workflow_key, [])
    packet_path = Path(action["review_packet_file"])
    packet = json.loads(packet_path.read_text())
    assert packet.pop("invoice_linkage_contract") == "oa_record:1"
    json_artifact.write_json_atomic(packet_path, packet)
    with pytest.raises(ValueError, match="frozen_packet_changed"):
        engine.validate_review_decision(Path(action["action_file"]), decision)


def test_legacy_marker_free_packet_without_link_fields_remains_readable(
    tmp_path: Path,
) -> None:
    engine, action, decision = _review(tmp_path, "business_entertainment", [])
    # Build a synthetic pre-contract frozen fixture; no historical artifacts are changed.
    packet_path = Path(action["review_packet_file"])
    packet = json.loads(packet_path.read_text())
    state_path = Path(action["state_file"])
    state = json.loads(state_path.read_text())
    material_path = Path(packet["material_file"])
    material = json.loads(material_path.read_text())
    del material["invoice_linkage_contract"]
    json_artifact.write_json_atomic(material_path, material)
    fingerprint = "sha256:" + hashlib.sha256(material_path.read_bytes()).hexdigest()
    state["material_fingerprint"] = packet["material_fingerprint"] = fingerprint
    del packet["invoice_linkage_contract"]
    json_artifact.write_json_atomic(state_path, state)
    json_artifact.write_json_atomic(packet_path, packet)
    engine.validate_review_decision(Path(action["action_file"]), decision)
    path = tmp_path / "candidate.json"
    json_artifact.write_json_atomic(path, decision)
    closed = engine.complete_review(Path(action["action_file"]), path)
    assert closed["execution_state"] == "closed"
    assert closed["business_state"] == "pass"


def test_frozen_material_contract_cannot_be_deleted_to_bypass_link_guard(
    tmp_path: Path,
) -> None:
    engine, action, decision = _review(tmp_path, "business_entertainment", [])
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    material_path = Path(packet["material_file"])
    material = json.loads(material_path.read_text())
    del material["invoice_linkage_contract"]
    json_artifact.write_json_atomic(material_path, material)
    with pytest.raises(ValueError, match="frozen_material_changed"):
        engine.validate_review_decision(Path(action["action_file"]), decision)


def test_link_guard_does_not_hide_inconsistent_model_overall(tmp_path: Path) -> None:
    engine, action, decision = _review(tmp_path, "business_entertainment", [])
    decision["items"][0].update(status="uncertain", missing_evidence=["付款状态未确认"])
    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.validate_review_decision(Path(action["action_file"]), decision)


def test_authoritative_invoice_content_does_not_replace_oa_association(
    tmp_path: Path,
) -> None:
    facts = [
        {
            "source": "invoice_cloud",
            "name": "发票购买方",
            "value": "示例公司",
            "subject_ref": "12345678901234567890",
        }
    ]
    engine, action, decision = _review(
        tmp_path, "business_entertainment", [_linked_field("unresolved")], facts=facts
    )
    item = next(
        item for item in decision["items"] if item["item_key"] == "invoice_linkage"
    )
    item["evidence_refs"] = [
        "material.authority_facts[0]",
        "material:resources.uploaded-invoice",
    ]
    path = tmp_path / "candidate.json"
    json_artifact.write_json_atomic(path, decision)
    closed = engine.complete_review(Path(action["action_file"]), path)
    assert closed["business_state"] == "needs_human"
