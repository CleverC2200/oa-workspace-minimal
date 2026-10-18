from __future__ import annotations

# Select the retained supplier explicitly for recorded CLI/slot behavior.
from tests.finance_preaudit.review_profiles import retained_deepseek_cli  # noqa: F401

import hashlib
import json
from pathlib import Path

import pytest

from apps.oa_approval import flow
from apps.oa_finance_preaudit import (
    codex_review_worker,
    flow_engine,
    manual_handoff,
    observability,
    production_pending,
    reconciliation,
    runtime_maintenance,
)


WORKFLOW_KEY = "tpm_small_marketing_fee"
RULE_VERSION = "tpm_small_marketing_fee:4"
ITEM_KEYS = (
    "attachment_completeness",
    "linked_invoice_receipt",
    "reimbursable_minimum",
    "entity_consistency",
)


def _company_fields() -> list[dict]:
    return [{"code": code, "name": name, "path": f"main[0].field[{index}]",
             "value": "测试公司", "read_status": "present", "value_source": "display"}
            for index, (code, name) in enumerate((("zfgs", "支付公司"), ("bxgs", "报销公司")))]


def _company_facts() -> list[dict]:
    return [{"source": "invoice_cloud", "name": "发票购买方", "value": "测试公司", "subject_ref": "test-invoice"}]


def _engine(
    tmp_path: Path,
    *,
    declared_fields: object | None = None,
) -> flow_engine.FlowEngine:
    current_fields = list(declared_fields) if isinstance(declared_fields, list) else []
    if not any(row.get("code") == "glfp" for row in current_fields):
        current_fields.append({"path": "detail[0].row[0].field[17]", "name": "关联发票", "code": "glfp",
                               "read_status": "present", "value": "12345678901234567890"})
    return flow_engine.FlowEngine(
        root=tmp_path / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "tpm-marketing-request",
                "received_at": "2026-09-16T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": request_id,
            "workflow_key": WORKFLOW_KEY,
            "workflow_name": "TPM小额市场费用报销申请流程",
            "declared_fields": current_fields + _company_fields(),
            "resources": [],
            "prepared": {
                "images": [],
                "structured_contents": [],
                "manifest": [],
            },
            "authority_facts": _company_facts(),
            "warnings": [],
        },
    )


def _decision(action: dict, *, conclusion: str) -> dict:
    items = []
    for item_key in ITEM_KEYS:
        status = "uncertain" if item_key == "attachment_completeness" else "pass"
        items.append(
            {
                "item_key": item_key,
                "status": status,
                "conclusion": conclusion if status == "uncertain" else "已核对",
                "evidence_refs": (
                    []
                    if status == "uncertain"
                    else ["material:declared_fields.expense_subject"]
                ),
                "missing_evidence": (
                    ["缺少具体附件"] if status == "uncertain" else []
                ),
            }
        )
    decision = {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "action_token": action["action_token"],
        "workflow_key": WORKFLOW_KEY,
        "rule_version": RULE_VERSION,
        "overall": "needs_human",
        "items": items,
    }
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    if "company_comparison_contract" in packet:
        decision["company_comparison"] = {"observed_refs": [row["evidence_ref"] for row in packet["company_comparison_contract"]["records"]],
                                          "result": "match", "reason": "报销公司测试公司、支付公司测试公司、购买方测试公司一致。"}
        decision["items"][-1]["conclusion"] = decision["company_comparison"]["reason"]
    return decision


def _image_engine(tmp_path: Path) -> flow_engine.FlowEngine:
    """带一张可查看原图的 TPM 引擎，用于验证第 1 项的图片证据要求。"""

    def collect(request_id: str, attempt_root: Path) -> dict:
        attachment = (
            attempt_root
            / "attachments"
            / WORKFLOW_KEY
            / request_id
            / "111_现场.jpg"
        )
        attachment.parent.mkdir(parents=True, exist_ok=True)
        attachment.write_bytes(b"attachment-bytes")
        prepared_image = attempt_root / "prepared" / "111" / "page-1.png"
        prepared_image.parent.mkdir(parents=True, exist_ok=True)
        prepared_image.write_bytes(b"page-bytes")
        return {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": request_id,
            "workflow_key": WORKFLOW_KEY,
            "workflow_name": "TPM小额市场费用报销申请流程",
            "declared_fields": _company_fields() + [
                {
                    "code": "subject",
                    "name": "预算科目",
                    "path": "detail[0].row[0].field[4]",
                    "read_status": "present",
                    "value": "终端建设费-终端形象制作费",
                }
            ],
            "resources": [
                {
                    "request_id": request_id,
                    "resource_id": "111",
                    "source_resource_id": "111",
                    "source_system": "oa",
                    "name": "现场.jpg",
                    "local_readonly_path": str(attachment),
                }
            ],
            "prepared": {
                "images": [
                    {
                        "kind": "image",
                        "name": "现场.jpg",
                        "resource_id": "111",
                        "parent_resource_id": "111",
                        "path": str(prepared_image),
                        "content_sha256": hashlib.sha256(
                            prepared_image.read_bytes()
                        ).hexdigest(),
                        "source_content_sha256": hashlib.sha256(
                            attachment.read_bytes()
                        ).hexdigest(),
                    }
                ],
                "structured_contents": [],
                "manifest": [
                    {
                        "resource_id": "111",
                        "name": "现场.jpg",
                        "status": "image",
                        "image_count": 1,
                    }
                ],
            },
            "authority_facts": _company_facts(),
            "warnings": [],
        }

    return flow_engine.FlowEngine(
        root=tmp_path / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "tpm-marketing-request",
                "received_at": "2026-09-16T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=collect,
    )


def _all_pass_decision(action: dict, *, attachment_refs: list[str]) -> dict:
    decision = _decision(action, conclusion="已核对")
    decision["overall"] = "pass"
    for item in decision["items"]:
        item.update(
            status="pass",
            conclusion="已核对",
            evidence_refs=["material:declared_fields.detail[0].row[0].field[4]"],
            missing_evidence=[],
        )
    attachment = decision["items"][0]
    attachment["conclusion"] = "附件齐全，符合当前科目要求。"
    attachment["evidence_refs"] = [
        "material:declared_fields.detail[0].row[0].field[20]",
        "material:resources.111",
        *attachment_refs,
    ]
    decision["items"][-1]["conclusion"] = decision["company_comparison"]["reason"]
    return decision


def test_tpm_attachment_pass_without_image_evidence_is_downgraded(
    tmp_path: Path,
) -> None:
    engine = _image_engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(
        json.dumps(_all_pass_decision(action, attachment_refs=[]), ensure_ascii=False)
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    remark = Path(closed["remark_file"]).read_text()
    assert closed["business_state"] == "needs_human"
    assert (
        "⚠️ 1、附件完整性：仅核对了附件名称与上传情况，未按清单与照片内容核对，请财务核对原单。"
        in remark
    )
    assert "AI审核结论：需人工判断 ⚠️" in remark
    # 冻结的 model 决定保持原样（receipt 指纹绑定它），降级只体现在公开意见与业务结论上。
    frozen = json.loads(Path(closed["decision_file"]).read_text())
    assert frozen["items"][0]["status"] == "pass"


def test_tpm_image_evidence_does_not_replace_price_evidence(tmp_path: Path) -> None:
    engine = _image_engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(
        json.dumps(
            _all_pass_decision(
                action, attachment_refs=["material:prepared.images.111.1"]
            ),
            ensure_ascii=False,
        )
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    remark = Path(closed["remark_file"]).read_text()
    assert closed["business_state"] == "needs_human"
    assert "⚠️ 1、附件完整性：参考报价未加载成功" in remark
    assert flow_engine._TPM_IMAGE_EVIDENCE_GAP not in remark


def test_tpm_image_evidence_rule_ignores_materials_without_images() -> None:
    item = {
        "item_key": "attachment_completeness",
        "status": "pass",
        "conclusion": "附件齐全，符合当前科目要求。",
        "evidence_refs": ["material:resources.111"],
        "missing_evidence": [],
    }

    flow_engine.enforce_tpm_attachment_image_evidence(
        [item], {"prepared": {"images": []}}
    )

    assert item["status"] == "pass"
    assert item["missing_evidence"] == []


def test_tpm_image_evidence_rule_leaves_non_pass_items_alone() -> None:
    item = {
        "item_key": "attachment_completeness",
        "status": "fail",
        "conclusion": "缺少盖章制作清单。",
        "evidence_refs": ["material:resources.111"],
        "missing_evidence": ["缺少盖章制作清单"],
    }

    flow_engine.enforce_tpm_attachment_image_evidence(
        [item], {"prepared": {"images": [{"name": "现场.jpg"}]}}
    )

    assert item["status"] == "fail"
    assert item["conclusion"] == "缺少盖章制作清单。"


def test_tpm_image_evidence_rule_is_idempotent() -> None:
    item = {
        "item_key": "attachment_completeness",
        "status": "pass",
        "conclusion": "附件齐全，符合当前科目要求。",
        "evidence_refs": ["material:resources.111"],
        "missing_evidence": [],
    }
    material = {"prepared": {"images": [{"name": "现场.jpg"}]}}

    flow_engine.enforce_tpm_attachment_image_evidence([item], material)
    flow_engine.enforce_tpm_attachment_image_evidence([item], material)

    assert item["status"] == "uncertain"
    assert item["missing_evidence"] == [flow_engine._TPM_IMAGE_EVIDENCE_GAP]


def test_tpm_and_ordinary_marketing_use_distinct_exact_identities() -> None:
    configs = flow.load_all_configs()
    rows = [
        {
            "request_id": "tpm-request",
            "workflow_id": "195039",
            "workflow_name": "TPM小额市场费用报销申请流程",
            "received_at": "2026-09-16T09:00:00+08:00",
        },
        {
            "request_id": "ordinary-request",
            "workflow_id": "125026",
            "workflow_name": "小额市场费用报销申请流程",
            "received_at": "2026-09-16T09:01:00+08:00",
        },
    ]

    identified = production_pending.identify_pending_rows(rows, configs)

    assert [item["flow_key"] for item in identified] == [
        WORKFLOW_KEY,
        "small_marketing_fee",
    ]


def test_tpm_is_registered_across_every_runtime_boundary() -> None:
    assert WORKFLOW_KEY in flow_engine.WORKFLOW_KEYS
    assert WORKFLOW_KEY in codex_review_worker._FINANCE_RULES
    assert WORKFLOW_KEY in manual_handoff.WORKFLOWS
    assert WORKFLOW_KEY in observability._WORKFLOW_KEYS
    assert WORKFLOW_KEY in reconciliation._WORKFLOW_KEYS
    assert WORKFLOW_KEY in runtime_maintenance._WORKFLOW_KEYS


def test_tpm_flow_freezes_a_four_item_contract(tmp_path: Path) -> None:
    action = _engine(tmp_path).next_action(WORKFLOW_KEY)
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )

    assert packet["workflow_key"] == WORKFLOW_KEY
    assert packet["rule_version"] == RULE_VERSION
    assert action["rule_version"] == RULE_VERSION
    assert json.loads(Path(action["state_file"]).read_text())["rule_version"] == (
        RULE_VERSION
    )
    assert tuple(item["item_key"] for item in packet["items"]) == ITEM_KEYS
    assert flow_engine._FLOW_SPECS["small_marketing_fee"]["rule_version"] == (
        "small_marketing_fee:2"
    )


def test_tpm_worker_uses_only_the_tpm_rule_pack(tmp_path: Path) -> None:
    action = _engine(tmp_path).next_action(WORKFLOW_KEY)
    action_file = Path(action["action_file"])

    schema = codex_review_worker._decision_schema(action_file)
    prompt = codex_review_worker._prompt(action_file)

    assert schema["properties"]["workflow_key"]["const"] == WORKFLOW_KEY
    assert schema["properties"]["rule_version"]["const"] == RULE_VERSION
    assert "TPM小额市场费生产预审" in prompt
    assert "tpm-small-marketing-fee.md" in prompt
    assert "四项" in prompt
    assert "第六项" not in prompt
    assert "六项" not in prompt
    assert "`references/flows/small-marketing-fee.md`" not in prompt


def test_tpm_read_failure_never_enters_reviewer_prompt(
    tmp_path: Path,
) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "tpm-marketing-request",
                "received_at": "2026-09-16T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda _request_id, _attempt: (_ for _ in ()).throw(
            RuntimeError("attachment_content_incomplete")
        ),
    )
    action = engine.next_action(WORKFLOW_KEY)

    assert action["action"] == "needs_admin"
    assert action["failure_detail_code"] == "attachment_content_incomplete"
    with pytest.raises(ValueError, match="frozen_packet_changed"):
        codex_review_worker._prompt(Path(action["action_file"]))


def test_tpm_content_incomplete_failure_keeps_redacted_stage_diagnostic(tmp_path):
    def _prepare_one_attachment(_request_id, _attempt_root):
        raise RuntimeError("attachment_content_incomplete: account=private token=secret")

    engine = flow_engine.FlowEngine(
        root=tmp_path / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda workflow_key: [{
            "request_id": "diagnostic-request",
            "received_at": "2026-09-16T09:00:00+08:00",
            "workflow_key": workflow_key,
        }],
        collect_material=_prepare_one_attachment,
    )
    action = engine.next_action(WORKFLOW_KEY)

    diagnostic = action["failure_diagnostic"]
    assert diagnostic == {
        "kind": "preaudit_material_failure_diagnostic",
        "code": "attachment_content_incomplete",
        "stage": "attachment_prepare",
        "error_class": "RuntimeError",
        "last_frame": "_prepare_one_attachment",
    }
    serialized = json.dumps(diagnostic, ensure_ascii=False)
    assert "account=private" not in serialized
    assert "secret" not in serialized


@pytest.mark.parametrize("all_pass", [True, False])
def test_tpm_public_remark_keeps_all_four_checks(tmp_path: Path, all_pass: bool) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    decision = _decision(action, conclusion="第15条现场照片可读取，仍缺少定位信息")
    if all_pass:
        decision["overall"] = "pass"
        for item in decision["items"]:
            item.update(status="pass", conclusion="已核对", evidence_refs=["material:declared_fields.expense_subject"], missing_evidence=[])
        decision["items"][-1]["conclusion"] = decision["company_comparison"]["reason"]
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(json.dumps(decision, ensure_ascii=False))
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    assert closed["write_state"] == "not_requested"
    assert closed["business_state"] == ("pass" if all_pass else "needs_human")
    remark = Path(closed["remark_file"]).read_text()
    assert len(remark.splitlines()) == 5
    for number, name in enumerate(("附件完整性", "发票关联与金额", "金额孰低核销", "支付公司一致"), 1):
        assert f"{number}、{name}：" in remark
    assert remark.count("✅") >= (4 if all_pass else 3)
    if not all_pass:
        assert "⚠️ 1、附件完整性：第15条现场照片可读取，仍缺少定位信息" in remark


@pytest.mark.parametrize("fault", ["extra_item", "missing_item", "reordered", "old_version", "missing_ref", "missing_gap", "overall"])
def test_tpm_shared_contract_still_rejects_invalid_decisions(tmp_path: Path, fault: str) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    decision = _decision(action, conclusion="第15条缺少定位照片")
    if fault == "extra_item":
        decision["items"].append({**decision["items"][0], "item_key": "payment_evidence"})
    elif fault == "missing_item":
        decision["items"].pop()
    elif fault == "reordered":
        decision["items"].reverse()
    elif fault == "old_version":
        decision["rule_version"] = "tpm_small_marketing_fee:2"
    elif fault == "missing_ref":
        decision["items"][1]["evidence_refs"] = []
    elif fault == "missing_gap":
        decision["items"][0]["missing_evidence"] = []
    else:
        decision["overall"] = "pass"
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(json.dumps(decision))
    with pytest.raises(ValueError):
        engine.complete_review(Path(action["action_file"]), decision_file)
    assert not Path(action["action_file"]).with_name("remark.txt").exists()


@pytest.mark.parametrize("version", [None, "tpm_small_marketing_fee:2"])
@pytest.mark.parametrize("degraded", [False, True])
def test_tpm_old_packets_remain_readable_but_cannot_resume(
    tmp_path: Path, version: str | None, degraded: bool,
) -> None:
    old_items = [
        {"item_key": key, "number": number, "name": name}
        for number, (key, name) in enumerate((
            ("expense_subject", "费用科目"),
            ("attachment_completeness", "科目附件完整性"),
            ("linked_invoice_receipt", "关联发票与收据"),
            ("payment_evidence", "支付凭证"),
            ("reimbursable_minimum", "金额孰低核销"),
            ("entity_consistency", "主体一致性"),
        ), 1)
    ]
    current = {"rule_version": RULE_VERSION, "items": [], "material_state": "degraded" if degraded else "complete",
               "required_uncertain_items": list(ITEM_KEYS) if degraded else []}
    state = {"rule_version": version} if version else {}
    old = flow_engine._accepted_tpm_small_marketing_packets(current, state)[0]
    assert old["rule_version"] == (version or "tpm_small_marketing_fee:1")
    assert old["items"] == old_items
    assert old["required_uncertain_items"] == ([item["item_key"] for item in old_items] if degraded else [])
    with pytest.raises(ValueError, match="frozen_packet_changed"):
        flow_engine._accepted_tpm_small_marketing_packets(current, state, action=state)


def test_tpm_obsolete_rules_rejected_before_starting_worker(tmp_path: Path, monkeypatch) -> None:
    action = _engine(tmp_path).next_action(WORKFLOW_KEY)
    packet_path = Path(action["review_packet_file"])
    packet = json.loads(packet_path.read_text())
    packet["rule_version"] = "tpm_small_marketing_fee:2"
    packet_path.write_text(json.dumps(packet))
    monkeypatch.setattr(codex_review_worker, "_decision_schema", lambda *a, **k: {})
    def unexpected_spawn(*args, **kwargs):
        pytest.fail("Obsolete rules must not start a child process")
    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", unexpected_spawn)
    result = codex_review_worker._execute_codex_raw(tmp_path, Path(action["action_file"]), 1, stderr_summary={})
    assert result.error_code == "codex_review_task_invalid"
    assert result.process_state == "not_started"


@pytest.mark.parametrize("target", ["action_file", "state_file", "review_packet_file"])
def test_tpm_current_version_cannot_be_removed_or_downgraded(tmp_path: Path, target: str) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    path = Path(action[target])
    value = json.loads(path.read_text())
    value.pop("rule_version")
    path.write_text(json.dumps(value))
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(json.dumps(_decision(action, conclusion="缺少现场定位照片")))
    result = engine.complete_review(Path(action["action_file"]), decision_file)
    assert result["action"] == "needs_admin"
    assert not Path(action["action_file"]).with_name("remark.txt").exists()


@pytest.mark.parametrize("write_state", ["submitting", "unknown"])
def test_tpm_obsolete_pending_write_stays_unknown(tmp_path: Path, monkeypatch, write_state: str) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    action_path = Path(action["action_file"])
    state_path = Path(action["state_file"])
    state = json.loads(state_path.read_text())
    state.update(rule_version="tpm_small_marketing_fee:2", write_state=write_state)
    state_path.write_text(json.dumps(state))
    action.update(rule_version="tpm_small_marketing_fee:2", write_state=write_state)
    action_path.write_text(json.dumps(action))
    def unexpected_recovery(*args, **kwargs):
        pytest.fail("Old pending write requires reconciliation, not automatic recovery")
    monkeypatch.setattr(engine, "_recover_frozen_submission", unexpected_recovery)
    result = engine._recover_completion(action_path.parent, state)
    assert result["action"] == "needs_admin"
    assert result["write_state"] == "unknown"
    assert result["reason"] == "writer_result_unknown"
    assert json.loads(state_path.read_text())["write_state"] == "unknown"
    assert not action_path.with_name("remark.txt").exists()
