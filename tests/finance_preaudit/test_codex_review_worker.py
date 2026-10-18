from __future__ import annotations

from apps.oa_finance_preaudit import attempt_state

import json
import io
import hashlib
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from tests.finance_preaudit.review_profiles import retained_deepseek_cli  # noqa: F401

from apps import json_artifact
from apps.oa_finance_preaudit import (
    codex_review_worker,
    flow_engine,
    review_failure_evidence,
    read_material,
    host_chat_review,
)
from apps.oa_approval import runtime_control


_SMALL_MARKETING_V2_ITEM_KEYS = [
    "budget_account",
    "attachment_completeness",
    "cost_bearer",
    "sap_customer_code_match",
    "reimbursable_minimum",
    "payment_company_consistency",
]


def _small_marketing_v2_decision():
    return {"overall": "needs_human", "items": {
        key: {"status": "uncertain", "conclusion": "支付截图和发票待核实。",
              "evidence_refs": [], "missing_evidence": ["待核实当前材料"]}
        for key in reversed(_SMALL_MARKETING_V2_ITEM_KEYS)
    }}


def _task(
    tmp_path: Path,
    *,
    workflow_key: str = "travel_expense",
) -> tuple[dict, Path]:
    project_root = tmp_path / "project"
    attempt_root = (
        project_root
        / "runtime"
        / "finance_preaudit_flows"
        / workflow_key
        / "cases"
        / ("a" * 64)
        / "attempt-1"
    )
    action_file = attempt_root / "action.json"
    attempt_root.mkdir(parents=True)
    json_artifact.write_json_atomic(
        action_file,
        {
            "schema_version": 1,
            "kind": "preaudit_action",
            "action": "review_required",
            "execution_state": "review_required",
            "business_state": "not_available",
            "write_state": "authorized",
            "workflow_key": workflow_key,
            "mode": "controlled_write",
            "write_authorized": True,
            "action_token": "test-action-token",
        },
    )
    json_artifact.write_json_atomic(
        action_file.with_name("state.json"),
        attempt_state.read_action(action_file),
    )
    json_artifact.write_json_atomic(
        action_file.with_name("review-packet.json"),
        {
            "workflow_key": workflow_key,
            "rule_version": f"{workflow_key}:1",
        },
    )
    return {
        "project_root": str(project_root),
        "action_file": str(action_file),
        "timeout_seconds": 900,
    }, action_file


def _small_marketing_v2_task(
    tmp_path: Path,
    *,
    budget_account: str,
) -> tuple[dict, Path]:
    project_root = tmp_path / "project"
    engine = flow_engine.FlowEngine(
        root=project_root / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "marketing-request",
                "received_at": "2026-09-04T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": request_id,
            "workflow_key": "small_marketing_fee",
            "workflow_name": "小额市场费报销流程",
            "declared_fields": {"budget_account": budget_account},
            "resources": [],
            "prepared": {
                "images": [],
                "structured_contents": [],
                "manifest": [],
            },
            "authority_facts": [],
            "warnings": [],
        },
    )
    action = engine.next_action("small_marketing_fee")
    action_file = Path(action["action_file"])
    return {
        "project_root": str(project_root),
        "action_file": str(action_file),
        "timeout_seconds": 900,
    }, action_file


def test_non_travel_schema_and_timing_follow_bound_action(tmp_path: Path) -> None:
    _task_payload, action_file = _task(
        tmp_path,
        workflow_key="business_entertainment",
    )

    schema = codex_review_worker._decision_schema(action_file)

    assert schema["properties"]["workflow_key"] == {
        "type": "string",
        "const": "business_entertainment",
    }
    assert schema["properties"]["rule_version"] == {
        "type": "string",
        "const": "business_entertainment:1",
    }
    assert codex_review_worker._write_agent_timing(
        action_file,
        wall_seconds=12.9,
        attempts=1,
        token_usage={"status": "unknown"},
    )
    timing = json.loads(
        action_file.with_name("agent-timing.json").read_text(encoding="utf-8")
    )
    assert timing["workflow_key"] == "business_entertainment"
    prompt = codex_review_worker._prompt(action_file)
    assert "业务招待费生产预审" in prompt
    assert "business-entertainment.md" in prompt
    assert "page_routing" not in prompt


@pytest.mark.parametrize("workflow_key", codex_review_worker._FINANCE_RULES)
def test_host_request_reads_complete_material_view_for_every_finance_workflow(
    tmp_path, monkeypatch, workflow_key,
):
    task, action_file = _task(tmp_path, workflow_key=workflow_key)
    material = {
        "workflow_key": workflow_key,
        "declared_fields": [
            {"path": "details[0].amount", "value": "123.45", "read_status": "present"},
            {"path": "details[1].amount", "value": None, "read_status": "unresolved"},
            {"path": "details[2].amount", "read_status": "empty"},
        ],
        "prepared": {"images": [{"path": "first.png"}, {"path": "second.png"}]},
        "invoice_evidence": {"invoices": [{"data_id": "invoice-record", "body_read_status": "not_read"}]},
    }
    material_file = action_file.with_name("material.json")
    json_artifact.write_json_atomic(material_file, material)
    packet_file = action_file.with_name("review-packet.json")
    json_artifact.write_json_atomic(packet_file, {
        "workflow_key": workflow_key,
        "rule_version": "tpm_small_marketing_fee:4" if workflow_key == "tpm_small_marketing_fee" else f"{workflow_key}:1",
        "material_file": str(material_file),
        "material_fingerprint": flow_engine._file_fingerprint(material_file),
    })
    action = attempt_state.read_action(action_file)
    json_artifact.write_json_atomic(action_file, {**action, "review_packet_file": str(packet_file)})
    json_artifact.write_json_atomic(action_file.with_name("state.json"), {**action, "review_packet_file": str(packet_file)})
    prompts = []

    def publish(action, *, prompt, timeout_seconds):
        assert action == action_file
        assert timeout_seconds == task["timeout_seconds"]
        prompts.append(prompt)
        return action_file.with_name("host-review-request.json")

    monkeypatch.setattr(host_chat_review, "selected", lambda: True)
    monkeypatch.setattr(host_chat_review, "publish", publish)
    monkeypatch.setattr(host_chat_review, "await_result", lambda _: {"status": "recorded"})
    assert host_chat_review.review(task) == {"status": "recorded"}
    prompt, = prompts
    assert f"python3 -m apps.oa_finance_preaudit.read_material {action_file}" in prompt
    assert "完整读完视图即完成材料文本读取，无需再重复展开同一 material.json" in prompt
    assert "审核包、流程规则和必要附件内容仍须完整读取" in prompt
    assert "DeepSeek HTTP API" in prompt
    assert "不通过看图工具或附件上传把原文件传入本审核会话" in prompt
    assert "完整读取 `references/payment-screenshot-exemption.md`" in prompt
    assert "body_read_status=not_read 表示正文未读取" in prompt
    assert "prepared.images" in prompt
    assert read_material.read(action_file) == read_material.render(json.loads(material_file.read_text()))


def _mock_final_output(monkeypatch, raw):
    monkeypatch.setenv("OA_REVIEW_API_KEY", "unit-test-key")
    prompts = []

    class Process:
        pid = 12345
        returncode = 0

        def __init__(self):
            self.stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")

        def communicate(self, **kwargs):
            prompts.append(kwargs["input"])

        def poll(self):
            return self.returncode

    def popen(command, **kwargs):
        payload = raw if isinstance(raw, bytes) else raw.encode()
        Path(command[command.index("--output-last-message") + 1]).write_bytes(payload)
        kwargs["stdout"].write('{"type":"turn.completed"}\n')
        kwargs["stdout"].flush()
        return Process()

    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", popen)
    monkeypatch.setattr(codex_review_worker.runtime_control, "stop_requested", lambda: False)
    return prompts


def _real_final_output(monkeypatch, raw, *, mutation="", usage=None):
    monkeypatch.setenv("OA_REVIEW_API_KEY", "unit-test-key")
    payload = raw if isinstance(raw, bytes) else raw.encode()
    event = {"type": "turn.completed"}
    if usage is not None:
        event["usage"] = usage
    script = (
        "import json,sys; from pathlib import Path; "
        f"Path(sys.argv[1]).write_bytes({payload!r}); "
        + mutation
        + f"print({json.dumps(event)!r}, flush=True)"
    )
    monkeypatch.setattr(
        codex_review_worker, "_codex_command",
        lambda project, schema, decision, visual_aux=None: [sys.executable, "-c", script, str(decision), str(schema)],
    )


@pytest.mark.parametrize("case", ["tool_arguments", "missing_overall", "missing_items", "status", "item_type", "binding_conflict"])
def test_cli_consumer_rejects_invalid_final_contract_from_real_child(tmp_path, monkeypatch, case):
    from tests.finance_preaudit.test_review_diagnostics import frozen_attempt
    from tests.finance_preaudit.test_flow_engine_submission import _needs_human_decision

    action_file = frozen_attempt(tmp_path)
    decision = _needs_human_decision(attempt_state.read_action(action_file))
    if case == "tool_arguments":
        decision = {"command": "read material"}
    elif case == "missing_overall":
        decision.pop("overall")
    elif case == "missing_items":
        decision.pop("items")
    elif case == "status":
        decision["items"][0]["status"] = "approved"
    elif case == "item_type":
        decision["items"][0] = "not an item"
    else:
        decision["action_token"] = "conflicting-token"
    raw = (json.dumps(decision) + "\r\n").encode()
    _real_final_output(monkeypatch, raw)
    task = {"project_root": str(tmp_path / "project"), "action_file": str(action_file), "timeout_seconds": 60}

    result = codex_review_worker.review(task)

    assert result["status"] == "failed"
    assert result["error_code"] == "codex_decision_invalid"
    assert result["token_usage"] == {"status": "unknown"}
    assert "decision" not in result
    retained = action_file.with_name(f"review-output-{hashlib.sha256(raw).hexdigest()}.txt")
    assert retained.read_bytes() == raw
    assert retained.stat().st_mode & 0o777 == 0o600
    rejection = json.loads(next(action_file.parent.glob("review-rejection-*.json")).read_text())
    assert rejection["validation_code"] == "decision_contract"
    process = json.loads(next(action_file.parent.glob("review-process-*.json")).read_text())
    assert process["process_state"] == "exited" and process["exit_code"] == 0
    assert not action_file.with_name("execution-receipt.json").exists()


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```"])
def test_cli_consumer_assembles_protocol_and_closes_needs_human_preview(tmp_path, monkeypatch, wrapper):
    from tests.finance_preaudit.test_review_diagnostics import frozen_attempt
    from tests.finance_preaudit.test_flow_engine_submission import _needs_human_decision

    action_file = frozen_attempt(tmp_path)
    expected = _needs_human_decision(attempt_state.read_action(action_file))
    business = {key: expected[key] for key in ("overall", "items")}
    _real_final_output(monkeypatch, wrapper.format(json.dumps(business)))
    task = {"project_root": str(tmp_path / "project"), "action_file": str(action_file), "timeout_seconds": 60}

    result = codex_review_worker.review(task)

    assert result["status"] == "reviewed" and result["decision"] == expected
    decision_file = tmp_path / "consumer-decision.json"
    json_artifact.write_json_atomic(decision_file, result["decision"])
    engine = flow_engine.FlowEngine(root=action_file.parents[4],
                                   discover_candidates=lambda _: pytest.fail("preview must use frozen inputs"),
                                   collect_material=lambda *_: pytest.fail("preview must use frozen inputs"))
    closed = engine.complete_review(action_file, decision_file)
    assert closed["action"] == "closed" and closed["business_state"] == "needs_human"
    assert closed["write_state"] == "not_requested"
    assert not list(action_file.parent.glob("review-rejection-*.json"))


@pytest.mark.parametrize("change", ["material", "new_identity", "corrupt_state"])
def test_cli_consumer_retains_original_binding_when_inputs_change(tmp_path, monkeypatch, change):
    from tests.finance_preaudit.test_review_diagnostics import frozen_attempt
    from tests.finance_preaudit.test_flow_engine_submission import _needs_human_decision

    action_file = frozen_attempt(tmp_path)
    original = attempt_state.read_action(action_file)
    reference = action_file.read_bytes()
    raw = json.dumps(_needs_human_decision(original)).encode()
    if change == "material":
        mutation = f"p=Path({str(action_file.with_name('material.json'))!r}); p.write_bytes(p.read_bytes()+b' '); "
    elif change == "corrupt_state":
        mutation = f"Path({str(action_file.with_name('state.json'))!r}).write_text('broken'); "
    else:
        mutation = "\n".join(
            f"p=Path({str(path)!r}); value=json.loads(p.read_text()); value['action_token']='different-token'; p.write_text(json.dumps(value))"
            for path in (action_file.with_name("state.json"),)
        ) + "\n"
    _real_final_output(monkeypatch, raw, mutation=mutation,
                       usage={"input_tokens": 12, "cached_input_tokens": 2, "output_tokens": 4})
    task = {"project_root": str(tmp_path / "project"), "action_file": str(action_file), "timeout_seconds": 60}

    result = codex_review_worker.review(task)

    assert result["error_code"] == "codex_decision_binding_invalid"
    assert result["status"] == "failed" and "decision" not in result
    assert result["token_usage"]["input_tokens"] == 12
    expected_binding = hashlib.sha256(original["action_token"].encode()).hexdigest()
    process = json.loads(next(action_file.parent.glob("review-process-*.json")).read_text())
    assert process["process_state"] == "exited" and process["exit_code"] == 0
    assert process["action_token_sha256"] == expected_binding
    rejection = json.loads(next(action_file.parent.glob("review-rejection-*.json")).read_text())
    assert rejection["action_token_sha256"] == expected_binding
    assert rejection["validation_code"] == "decision_binding"
    retained = action_file.with_name(f"review-output-{hashlib.sha256(raw).hexdigest()}.txt")
    assert retained.read_bytes() == raw and retained.stat().st_mode & 0o777 == 0o600
    context = json.loads(next(action_file.parent.glob("review-context-*.json")).read_text())
    assert context["schema"]["properties"]["action_token"]["const"] == original["action_token"]
    assert action_file.read_bytes() == reference
    assert context["input_fingerprints"]["action.json"] == hashlib.sha256(reference).hexdigest()
    if change != "material":
        assert context["input_fingerprints"]["state.json"] != hashlib.sha256(action_file.with_name("state.json").read_bytes()).hexdigest()
    assert not action_file.with_name("execution-receipt.json").exists()


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", " \n```json\r\n{}\r\n```\n"])
def test_finance_reviewer_consumes_single_json_fence(tmp_path, monkeypatch, wrapper):
    from tests.finance_preaudit.test_flow_engine_submission import _material, _needs_human_decision

    project_root = tmp_path / "project"
    engine = flow_engine.FlowEngine(
        root=project_root / "runtime/finance_preaudit_flows",
        discover_candidates=lambda key: [{"request_id": "synthetic", "received_at": "2026-08-22T09:00:00+08:00", "workflow_key": key}],
        collect_material=lambda request, _: _material(request),
    )
    action = engine.next_action("travel_expense")
    action_file = Path(action["action_file"])
    decision = _needs_human_decision(action)
    _mock_final_output(monkeypatch, wrapper.format(json.dumps(decision)))
    task = {"project_root": str(project_root), "action_file": str(action_file), "timeout_seconds": 60}
    result = codex_review_worker.review(task)
    assert result["status"] == "reviewed"
    assert result["decision"] == decision
    assert not list(action_file.parent.glob("review-rejection-*.json"))
    assert not action_file.with_name("execution-receipt.json").exists()
    assert not list(action_file.parent.glob("review-output-*.txt"))
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, result["decision"])
    closed = engine.complete_review(action_file, decision_file)
    assert closed["action"] == "closed"
    assert closed["business_state"] == "needs_human"
    assert closed["write_state"] == "not_requested"


@pytest.mark.parametrize("facts,payment_status,amount_status,payment,amount,gaps", [
    (
        ["食品订单和实付32元与来访招待明细可信对应，原票与申报均32元。"],
        "pass", "pass", "32元食品支出与当前招待费用对应。", "按32元核销。", [],
    ),
    (
        ["原订单80元与同明细原餐费票对应，收款方与销售方一致，成功实付70元、优惠10元；开票日比付款日晚一天，招待上下文一致。"],
        "pass", "pass", "70元餐费支出与当前招待费用对应。", "按优惠后实付70元核销。", [],
    ),
    (
        ["第1行经营付款80元与原餐费票同明细同日同额，招待上下文一致，收款展示名与销售方不同。",
         "第2行食品订单实付80元、同笔退款10元，原票80元、当前申报90元，费用对应已确认。",
         "第3行转款30元成功，但用途尚未确认；另有历史核定999元和里程读数，未提供当前里程支出和本流程适用依据。"],
        "uncertain", "fail", "三笔净支出180元已确认，第3行30元费用对应及里程支出、适用依据待核。",
        "第1、2行已确认可核销150元，第2行超报20元；第3行和里程费用待核，历史999元未计入。",
        ["第3行费用对应及里程支出、本流程适用依据"],
    ),
], ids=["food", "order-backed-invoice-date", "known-unknown-overclaim-history"])
def test_entertainment_worker_delivers_attribution_rule_and_preserves_preview(
    tmp_path, monkeypatch, facts, payment_status, amount_status, payment, amount, gaps,
):
    from tests.finance_preaudit.test_flow_engine_entertainment import _material, _decision

    project = tmp_path / "project"
    relative_rule = Path(".agents/skills/oa-finance-preaudit/references/flows/business-entertainment.md")
    rule = project / relative_rule
    rule.parent.mkdir(parents=True)
    rule.write_bytes((Path(__file__).resolve().parents[2] / relative_rule).read_bytes())

    def collect(request_id, _):
        material = _material(request_id)
        material["declared_fields"].extend({
            "path": f"detail[0].row[{index}].field[6]", "name": "消费事实", "value": fact,
        } for index, fact in enumerate(facts))
        return material

    engine = flow_engine.FlowEngine(
        root=project / "runtime/finance_preaudit_flows",
        discover_candidates=lambda key: [{"request_id": "synthetic-attribution",
            "workflow_key": key, "received_at": "2026-10-10T09:00:00+08:00"}],
        collect_material=collect,
    )
    action = engine.next_action("business_entertainment", mode="preview")
    action_file = Path(action["action_file"])
    frozen_material = action_file.with_name("material.json").read_bytes()
    decision = _decision(action)
    decision["overall"] = "pass" if amount_status == "pass" else "needs_human"
    decision["items"][0].update(status=payment_status, conclusion=payment, missing_evidence=gaps)
    decision["items"][2]["conclusion"] = "当前阶段按既定策略暂不校验，OA 暂未提供预算数据。"
    decision["items"][3].update(status=amount_status, conclusion=amount, missing_evidence=gaps)
    script = tmp_path / "recorded-reviewer.py"
    script.write_text(
        "import json,re,shlex,sys\nfrom pathlib import Path\n"
        "args=sys.argv[1:];prompt=sys.stdin.read()\n"
        + f"relative={str(relative_rule)!r}\n"
        "assert 'references/flows/business-entertainment.md' in prompt\n"
        "rule=Path(args[args.index('--cd')+1])/relative\n"
        + f"assert rule.read_bytes()=={rule.read_bytes()!r}\n"
        "assert '## 支付事实与费用归属' in rule.read_text()\n"
        + f"sys.path.insert(0,{str(Path(__file__).resolve().parents[2])!r})\n"
        "from apps.oa_finance_preaudit import read_material\n"
        "command=re.search(r'`(python3 -m apps.oa_finance_preaudit.read_material [^`]+)`',prompt)\n"
        "assert command\n"
        "view=read_material.read(Path(shlex.split(command.group(1))[-1]))\n"
        + f"assert all(fact in view for fact in {facts!r})\n"
        + "Path(args[args.index('--output-last-message')+1]).write_text("
        + repr(json.dumps(decision, ensure_ascii=False)) + ")\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))\n"
    )
    original_popen = subprocess.Popen
    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", lambda command, **kwargs:
                        original_popen([sys.executable, str(script), *command[1:]], **kwargs))
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")

    reviewed = codex_review_worker.review({
        "project_root": str(project), "action_file": str(action_file), "timeout_seconds": 60,
    })

    assert reviewed["status"] == "reviewed"
    assert reviewed["decision"] == decision
    decision_file = tmp_path / "entertainment-decision.json"
    json_artifact.write_json_atomic(decision_file, reviewed["decision"])
    closed = engine.complete_review(action_file, decision_file)
    assert closed["business_state"] == decision["overall"]
    assert closed["write_state"] == "not_requested"
    assert json.loads(Path(closed["decision_file"]).read_text()) == decision
    remark = Path(closed["remark_file"]).read_text()
    assert all(item["conclusion"] in remark for item in decision["items"])
    assert action_file.with_name("material.json").read_bytes() == frozen_material


@pytest.mark.parametrize("period_status,period_conclusion,period_gap", [
    ("uncertain", "申报周期8月21日至9月30日，8月20日预订住宿早于周期，实际入住待核实。", ["8月20日实际入住证据"]),
    ("fail", "申报周期8月21日至9月30日，已核实8月20日实际入住，超出周期。", []),
])
def test_travel_worker_preview_preserves_date_uncertainty_and_confirmed_anomalies(
    tmp_path, monkeypatch, period_status, period_conclusion, period_gap,
):
    from tests.finance_preaudit.test_flow_engine_submission import _material, _decision

    project_root = tmp_path / "project"
    relative_rule = Path(".agents/skills/oa-finance-preaudit/references/flows/travel-expense.md")
    rule_file = project_root / relative_rule
    rule_file.parent.mkdir(parents=True)
    rule_file.write_text((Path(__file__).resolve().parents[2] / relative_rule).read_text())

    def collect(request_id, _):
        material = _material(request_id)
        material["declared_fields"] = [
            {"path": "main.period", "name": "申报周期", "value": "2026-08-21至2026-09-30"},
            {"path": "detail.hotel", "name": "预订住宿", "value": "2026-08-20至2026-08-21"},
        ]
        material["authority_facts"] = [{
            "code": "recorded_hotel_checkin", "value": "2026-08-20" if period_status == "fail" else None,
        }]
        return material

    engine = flow_engine.FlowEngine(
        root=project_root / "runtime/finance_preaudit_flows",
        discover_candidates=lambda key: [{"request_id": "synthetic-travel-period", "workflow_key": key,
                                          "received_at": "2026-10-10T09:00:00+08:00"}],
        collect_material=collect,
    )
    action = engine.next_action("travel_expense", mode="preview")
    action_file = Path(action["action_file"])
    decision = _decision(action)
    decision["overall"] = "needs_human"
    decision["items"][0].update(status=period_status, conclusion=period_conclusion, missing_evidence=period_gap)
    decision["items"][4].update(status="fail", conclusion="8月20日夏邑超出两份负责区域记录。")

    prompts = _mock_final_output(monkeypatch, json.dumps(decision))
    reviewed = codex_review_worker.review({"project_root": str(project_root),
                                          "action_file": str(action_file), "timeout_seconds": 60})
    # Rule delivery and closure only; the real model must independently classify the dates.
    assert "仅有申报或预订住宿日期" in rule_file.read_text()
    assert "实际入住未确认" in rule_file.read_text()
    assert "差旅异常或待核事项的日期、地点不受精简限制" in prompts[0]
    assert reviewed["status"] == "reviewed"
    assert reviewed["decision"] == decision
    decision_file = tmp_path / "travel-decision.json"
    json_artifact.write_json_atomic(decision_file, reviewed["decision"])
    closed = engine.complete_review(action_file, decision_file)
    assert closed["business_state"] == "needs_human"
    assert closed["write_state"] == "not_requested"
    remark = Path(closed["remark_file"]).read_text()
    assert period_conclusion in remark
    assert "8月20日夏邑超出两份负责区域记录" in remark


@pytest.mark.parametrize("hotel_address,actual_stay,status,conclusion,gaps", [
    ("示例路29-6号", False, "uncertain", "9月22日考勤在示例市乙区，酒店地址仅有示例路29-6号，城市区县及实际入住待核。", ["9月22日酒店城市区县及实际入住"]),
    ("示例市甲区示例路29-6号", False, "uncertain", "9月22日预订酒店在示例市甲区，考勤在示例市乙区，实际入住待核。", ["9月22日实际入住"]),
    ("示例市甲区示例路29-6号", True, "fail", "9月22日实际住宿在示例市甲区，与示例市乙区考勤不一致。", []),
])
def test_travel_worker_delivers_address_boundary_and_preserves_verified_location_conflict(
    tmp_path, monkeypatch, hotel_address, actual_stay, status, conclusion, gaps,
):
    from tests.finance_preaudit.test_flow_engine_submission import _material, _decision

    project_root = tmp_path / "project"
    relative_rule = Path(".agents/skills/oa-finance-preaudit/references/flows/travel-expense.md")
    rule_file = project_root / relative_rule
    rule_file.parent.mkdir(parents=True)
    rule_file.write_text((Path(__file__).resolve().parents[2] / relative_rule).read_text())

    def collect(request_id, _):
        material = _material(request_id)
        material["declared_fields"] = [{"path": "detail.hotel", "name": "酒店名称",
                                       "value": "示例酒店（示例市甲区店）"}]
        material["authority_facts"] = [
            {"code": "recorded_hotel_address", "value": hotel_address},
            {"code": "recorded_attendance_address", "value": "示例市乙区"},
            {"code": "recorded_actual_stay", "value": "2026-09-22" if actual_stay else None},
        ]
        return material

    engine = flow_engine.FlowEngine(
        root=project_root / "runtime/finance_preaudit_flows",
        discover_candidates=lambda key: [{"request_id": "synthetic-hotel-address", "workflow_key": key,
                                          "received_at": "2026-10-10T09:00:00+08:00"}],
        collect_material=collect,
    )
    action = engine.next_action("travel_expense", mode="preview")
    action_file = Path(action["action_file"])
    decision = _decision(action)
    decision["overall"] = "needs_human"
    decision["items"][3].update(status=status, conclusion=conclusion, missing_evidence=gaps)
    prompts = _mock_final_output(monkeypatch, json.dumps(decision))
    reviewed = codex_review_worker.review({"project_root": str(project_root),
                                          "action_file": str(action_file), "timeout_seconds": 60})
    # Offline policy delivery only. Model geography must be checked against source facts separately.
    assert str(relative_rule).removeprefix(".agents/skills/oa-finance-preaudit/") in prompts[0]
    assert "城市、区县中缺失的行政层级保持未知" in rule_file.read_text()
    assert "地理常识" in rule_file.read_text()
    assert reviewed["status"] == "reviewed"
    decision_file = tmp_path / "hotel-decision.json"
    json_artifact.write_json_atomic(decision_file, reviewed["decision"])
    closed = engine.complete_review(action_file, decision_file)
    assert closed["business_state"] == "needs_human"
    assert closed["write_state"] == "not_requested"
    assert conclusion in Path(closed["remark_file"]).read_text()


@pytest.mark.parametrize("current_record_confirmed,car_locations,status,conclusion,gaps", [
    (False, "北京、石家庄", "uncertain", "北京、石家庄是否属于负责区域取决于尚未确认的适用版本，需核对。", ["两份负责区域记录的适用、生效或覆盖关系"]),
    (True, "北京", "fail", "权威来源明确现行负责区域为河北四市，北京属于区域外地点。", []),
    (False, "夏邑、苏州、上海", "fail", "夏邑、苏州、上海均位于两份负责区域之外。", []),
])
def test_travel_worker_delivers_area_version_policy_without_erasing_common_outside_locations(
    tmp_path, monkeypatch, current_record_confirmed, car_locations, status, conclusion, gaps,
):
    from tests.finance_preaudit.test_flow_engine_submission import _material, _decision

    project_root = tmp_path / "project"
    relative_rule = Path(".agents/skills/oa-finance-preaudit/references/flows/travel-expense.md")
    rule_file = project_root / relative_rule
    rule_file.parent.mkdir(parents=True)
    rule_file.write_text((Path(__file__).resolve().parents[2] / relative_rule).read_text())

    def collect(request_id, _):
        material = _material(request_id)
        material["declared_fields"] = [{"path": "detail.car", "name": "带车地点", "value": car_locations}]
        material["authority_facts"] = [
            {"name": "负责区域", "source": "recorded_authority", "observed_at": "2025-11-21",
             "value": "河北保定市、河北张家口市、河北廊坊市、河北沧州市"},
            {"name": "负责区域", "source": "recorded_authority", "observed_at": "2025-10-14",
             "value": "北京、天津、河北省、内蒙古"},
        ]
        if current_record_confirmed:
            material["authority_facts"].append({"name": "负责区域有效性", "source": "recorded_authority",
                "value": "权威来源明确河北四市记录现行有效，另一记录已被覆盖，适用于本次出差。"})
        return material

    engine = flow_engine.FlowEngine(
        root=project_root / "runtime/finance_preaudit_flows",
        discover_candidates=lambda key: [{"request_id": "synthetic-area-versions", "workflow_key": key,
                                          "received_at": "2026-10-10T09:00:00+08:00"}],
        collect_material=collect,
    )
    action = engine.next_action("travel_expense", mode="preview")
    action_file = Path(action["action_file"])
    decision = _decision(action)
    decision["overall"] = "needs_human"
    decision["items"][4].update(status=status, conclusion=conclusion, missing_evidence=gaps)
    prompts = _mock_final_output(monkeypatch, json.dumps(decision))
    reviewed = codex_review_worker.review({"project_root": str(project_root),
                                          "action_file": str(action_file), "timeout_seconds": 60})
    # Offline rule delivery and closure only; native financial acceptance remains separate.
    assert "references/flows/travel-expense.md" in prompts[0]
    assert "申请日期、记录日期或返回排序不证明当前生效" in rule_file.read_text()
    assert "全部可能适用记录之外" in rule_file.read_text()
    assert reviewed["status"] == "reviewed"
    decision_file = tmp_path / "area-decision.json"
    json_artifact.write_json_atomic(decision_file, reviewed["decision"])
    closed = engine.complete_review(action_file, decision_file)
    assert closed["business_state"] == "needs_human"
    assert closed["write_state"] == "not_requested"
    assert conclusion in Path(closed["remark_file"]).read_text()


@pytest.mark.parametrize("raw", [
    '说明\n```json\n{}\n```',
    '{}\n```json\n{}\n```',
    '```json\n{}\n```\n```json\n{}\n```',
    '```json\n{"overall":"pass","overall":"needs_human"}\n```',
    '{"item":{"status":"pass","status":"fail"}}',
    '{"amount":NaN}', '{"amount":Infinity}', '{"amount":-Infinity}',
    '{"amount":1e309}', '{"amount":-1e309}',
    '```json\n{"amount":1e309}\n```',
    '```json\n{"items":',
    'not-json\r\n', b'not-json\xff', '[]', 'null', '"private-value"',
])
def test_finance_reviewer_rejects_ambiguous_or_invalid_json(tmp_path, monkeypatch, raw):
    task, action_file = _task(tmp_path)
    _mock_final_output(monkeypatch, raw)
    result = codex_review_worker.review(task)
    assert result["status"] == "failed"
    assert result["error_code"] == "codex_decision_invalid"
    assert result["write_state"] == "not_requested"
    assert "decision" not in result
    assert len(list(action_file.parent.glob("review-rejection-*.json"))) == 1
    assert not action_file.with_name("execution-receipt.json").exists()
    outputs = list(action_file.parent.glob("review-output-*.txt"))
    assert len(outputs) == 1
    payload = raw if isinstance(raw, bytes) else raw.encode()
    assert outputs[0].read_bytes() == payload
    import hashlib
    assert outputs[0].name == f"review-output-{hashlib.sha256(payload).hexdigest()}.txt"
    assert outputs[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("damage", ["symlink", "content", "permissions", "directory"])
def test_failed_output_retention_refuses_changed_evidence(tmp_path, monkeypatch, damage):
    import hashlib

    task, action_file = _task(tmp_path)
    raw = "not-json"
    target = action_file.with_name(f"review-output-{hashlib.sha256(raw.encode()).hexdigest()}.txt")
    if damage == "symlink":
        target.symlink_to(tmp_path / "outside")
    elif damage == "directory":
        target.mkdir()
    elif damage == "permissions":
        target.write_text(raw)
        target.chmod(0o644)
    else:
        target.write_text("other evidence")
    _mock_final_output(monkeypatch, raw)
    result = codex_review_worker.review(task)
    assert result["error_code"] == "codex_decision_diagnostic_failed"
    assert result["write_state"] == "not_requested"
    assert not list(action_file.parent.glob("review-rejection-*.json"))
    if damage == "content":
        assert target.read_text() == "other evidence"
    if damage == "permissions":
        assert target.stat().st_mode & 0o777 == 0o644


def test_failed_output_retention_failure_remains_unwritten(tmp_path, monkeypatch):
    task, action_file = _task(tmp_path)
    _mock_final_output(monkeypatch, "not-json")

    original_write = runtime_control.write_control_bytes_atomic
    def unsafe(path, payload):
        if path.parent == action_file.parent:
            raise RuntimeError("control_file_unsafe")
        return original_write(path, payload)

    monkeypatch.setattr(codex_review_worker.runtime_control, "write_control_bytes_atomic", unsafe)
    result = codex_review_worker.review(task)
    assert result["error_code"] == "codex_decision_diagnostic_failed"
    assert result["write_state"] == "not_requested"
    assert not list(action_file.parent.glob("review-rejection-*.json"))
    assert "review-output-*.txt" in codex_review_worker._prompt(action_file)


def test_general_expense_worker_uses_bound_rule_pack(tmp_path: Path) -> None:
    _task_payload, action_file = _task(
        tmp_path,
        workflow_key="general_expense",
    )

    schema = codex_review_worker._decision_schema(action_file)
    prompt = codex_review_worker._prompt(action_file)

    assert schema["properties"]["workflow_key"] == {
        "type": "string",
        "const": "general_expense",
    }
    assert schema["properties"]["rule_version"] == {
        "type": "string",
        "const": "general_expense:1",
    }
    assert "通用费用报销生产预审" in prompt
    assert "general-expense.md" in prompt
    assert "实付0元，均为收到货后自动付款" in prompt
    assert "“先乘车后付款”不等于“先用后付”" in prompt
    assert "当前状态明确为支付成功" in prompt
    assert "三者一致，应选择当前实际公司全称支付" in prompt
    assert "page_routing" not in prompt


def test_general_expense_prompt_uses_shared_payment_exemption(tmp_path: Path) -> None:
    _, action_file = _task(tmp_path, workflow_key="general_expense")
    assert "完整读取 `references/payment-screenshot-exemption.md`" in codex_review_worker._prompt(action_file)


@pytest.mark.parametrize("amount_status,overall,amount_conclusion,attachment_gap", [
    ("pass", "pass", "整单核销金额为100.00元。", None),
    ("fail", "needs_human", "申报200元、实付190元，相差10元，整单核销上限190元。", None),
    ("fail", "needs_human", "本轮财务审核100元、同范围实付90元，相差10元，核销上限90元。", "实际交易收款对象与销售方的主体关系待核，费用及支付金额对应已确认。"),
])
def test_general_review_routes_payment_rules_and_preserves_preview_decision(
    tmp_path: Path, monkeypatch, amount_status: str, overall: str,
    amount_conclusion: str, attachment_gap: str | None,
) -> None:
    from tests.finance_preaudit.test_flow_engine_general_expense import _material

    project_root = tmp_path / "project"
    engine = flow_engine.FlowEngine(
        root=project_root / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda key: [{
            "request_id": "employee-reimbursement", "workflow_key": key,
            "received_at": "2026-10-10T09:00:00+08:00",
        }],
        collect_material=lambda request_id, _: _material(request_id),
    )
    action = engine.next_action("general_expense", mode="preview")
    action_file = Path(action["action_file"])
    # This records the model process boundary, not a substitute financial judge.
    # Actual model acceptance uses the same Worker and frozen preview input.
    recorded_decision = {
        "schema_version": 1, "kind": "preaudit_review_decision",
        "action_token": action["action_token"], "workflow_key": "general_expense",
        "rule_version": "general_expense:1", "overall": overall,
        "items": [
            {"item_key": key, "status": status, "conclusion": conclusion,
             "evidence_refs": ["material:declared_fields"], "missing_evidence": gaps}
            for key, status, conclusion, gaps in (
                ("attachment_payment_evidence", "uncertain" if attachment_gap else "pass",
                 attachment_gap or "附件及支付证据完整。", [attachment_gap] if attachment_gap else []),
                ("invoice_linkage", "pass", "发票均已关联。", []),
                ("budget", "pass", "当前阶段按既定策略暂不校验部门年度预算。", []),
                ("reimbursable_minimum", amount_status, amount_conclusion, []),
                ("payment_company_consistency", "pass", "报销公司、支付公司、购买方三者一致，应选择示例有限公司支付。", []),
            )
        ],
    }
    prompts = _mock_final_output(monkeypatch, json.dumps(recorded_decision))
    reviewed = codex_review_worker.review({
        "project_root": str(project_root), "action_file": str(action_file),
        "timeout_seconds": 60,
    })

    assert reviewed["status"] == "reviewed"
    assert len(prompts) == 1
    assert "完整读取 `references/payment-screenshot-exemption.md`" in prompts[0]
    assert "references/flows/general-expense.md" in prompts[0]
    assert "确认名称不一致后按规则核验人员关系和补充材料" not in prompts[0]
    assert reviewed["decision"] == recorded_decision
    decision_file = tmp_path / "recorded-decision.json"
    json_artifact.write_json_atomic(decision_file, reviewed["decision"])
    closed = engine.complete_review(action_file, decision_file)
    assert closed["action"] == "closed"
    assert closed["business_state"] == overall
    assert closed["write_state"] == "not_requested"
    remark = Path(closed["remark_file"]).read_text(encoding="utf-8")
    assert amount_conclusion in remark
    if attachment_gap:
        assert "⚠️ 1、附件完整性/支付证据：" in remark
        assert attachment_gap in remark
    else:
        assert "✅ 1、附件完整性/支付证据：附件及支付证据完整。" in remark
    if amount_status == "fail":
        assert "❌ 4、金额孰低核销：" in remark
    receipt = json.loads(action_file.with_name("execution-receipt.json").read_text())
    assert receipt["mode"] == "preview"
    assert receipt["write"] is False
    assert receipt["no_write_evidence"] == "confirmed_no_write"


def test_mixed_general_review_loads_subject_requirements_and_keeps_real_gap(
    tmp_path: Path, monkeypatch,
) -> None:
    from tests.finance_preaudit.test_flow_engine_general_expense import _material

    project_root = tmp_path / "project"
    relative_rule = Path(".agents/skills/oa-finance-preaudit/references/flows/general-expense.md")
    rule_file = project_root / relative_rule
    rule_file.parent.mkdir(parents=True)
    rule_file.write_text((Path(__file__).resolve().parents[2] / relative_rule).read_text())

    def collect_material(request_id, _):
        material = _material(request_id)
        material["declared_fields"].extend([
            {"path": "detail[0].row[0].field[0]", "name": "预算科目", "code": "yskm",
             "read_status": "present", "value": "办公费-邮寄费"},
            {"path": "detail[0].row[1].field[0]", "name": "预算科目", "code": "yskm",
             "read_status": "present", "value": "维修费"},
        ])
        return material

    engine = flow_engine.FlowEngine(
        root=project_root / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda key: [{
            "request_id": "synthetic-mixed-subjects", "workflow_key": key,
            "received_at": "2026-10-10T09:00:00+08:00",
        }],
        collect_material=collect_material,
    )
    action = engine.next_action("general_expense", mode="preview")
    action_file = Path(action["action_file"])
    recorded_decision = {
        "schema_version": 1, "kind": "preaudit_review_decision",
        "action_token": action["action_token"], "workflow_key": "general_expense",
        "rule_version": "general_expense:1", "overall": "needs_human",
        "items": [
            {"item_key": "attachment_payment_evidence", "status": "uncertain",
             "conclusion": "邮寄费发票与运单符合；维修费实际交易收款对象与发票销售方的关系待核。",
             "evidence_refs": ["material:declared_fields"],
             "missing_evidence": ["维修费实际交易收款对象与发票销售方的关系"]},
            {"item_key": "invoice_linkage", "status": "pass", "conclusion": "发票已关联。",
             "evidence_refs": ["material:declared_fields"], "missing_evidence": []},
            {"item_key": "budget", "status": "pass", "conclusion": "当前阶段按既定策略暂不校验部门年度预算。",
             "evidence_refs": ["material:declared_fields"], "missing_evidence": []},
            {"item_key": "reimbursable_minimum", "status": "uncertain", "conclusion": "维修费交易关系待核，核销金额暂不能确定。",
             "evidence_refs": ["material:declared_fields"], "missing_evidence": ["维修费支付归属"]},
            {"item_key": "payment_company_consistency", "status": "pass",
             "conclusion": "报销公司、支付公司、购买方三者一致，应选择示例有限公司支付。",
             "evidence_refs": ["material:declared_fields"], "missing_evidence": []},
        ],
    }

    prompts = _mock_final_output(monkeypatch, json.dumps(recorded_decision))
    reviewed = codex_review_worker.review({
        "project_root": str(project_root), "action_file": str(action_file), "timeout_seconds": 60,
    })
    assert reviewed["status"] == "reviewed"
    assert len(prompts) == 1
    assert "references/flows/general-expense.md" in prompts[0]
    # Contract guard only. Actual financial behavior is checked with the model.
    rule = rule_file.read_text()
    assert "混合单逐明细按各自科目要求核验" in rule
    assert "额外上传的支付截图不新增付款人姓名或交易收款对象与销售方关系的必需材料要求" in rule
    assert reviewed["decision"] == recorded_decision
    decision_file = tmp_path / "mixed-decision.json"
    json_artifact.write_json_atomic(decision_file, reviewed["decision"])
    closed = engine.complete_review(action_file, decision_file)
    assert closed["business_state"] == "needs_human"
    assert closed["write_state"] == "not_requested"
    remark = Path(closed["remark_file"]).read_text()
    assert "邮寄费发票与运单符合" in remark
    assert "维修费实际交易收款对象与发票销售方的关系待核" in remark


@pytest.mark.parametrize("gap,attachment_missing,amount_status,amount_conclusion,amount_missing", [
    ("附加服务费的金额与订单共享关系待核，已确认付款及退款事实保留。",
     ["附加服务费的金额与订单共享关系"], "uncertain",
     "已确认付款及退款事实保留，费用对应关系未核实，整单核销上限待核。",
     ["附加服务费的同范围金额对应关系"]),
    ("实际第三方收款者与当前费用及发票销售方的对应关系待核，已确认支付成功事实保留。",
     ["实际第三方收款者与当前费用及发票销售方的对应关系"], "uncertain",
     "已确认付款事实保留，费用对应关系未核实，整单核销上限待核。",
     ["第三方付款的费用及发票对应关系"]),
    ("定位照片未读取，费用与付款及发票的金额对应关系已确认。",
     ["定位照片"], "pass", "整单核销金额为100.00元。", []),
])
def test_general_review_preserves_payment_facts_without_closing_expense_relations(
    tmp_path: Path, monkeypatch, gap: str, attachment_missing: list[str],
    amount_status: str, amount_conclusion: str, amount_missing: list[str],
) -> None:
    from tests.finance_preaudit.test_flow_engine_general_expense import _material

    project_root = tmp_path / "project"
    references = Path(".agents/skills/oa-finance-preaudit/references")
    for name in ("observation-contract.md", "payment-screenshot-exemption.md"):
        target = project_root / references / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((Path(__file__).resolve().parents[2] / references / name).read_text())
    engine = flow_engine.FlowEngine(
        root=project_root / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda key: [{
            "request_id": "synthetic-expense-relation", "workflow_key": key,
            "received_at": "2026-10-10T09:00:00+08:00",
        }],
        collect_material=lambda request_id, _: _material(request_id),
    )
    action = engine.next_action("general_expense", mode="preview")
    action_file = Path(action["action_file"])
    recorded_decision = {
        "schema_version": 1, "kind": "preaudit_review_decision",
        "action_token": action["action_token"], "workflow_key": "general_expense",
        "rule_version": "general_expense:1", "overall": "needs_human",
        "items": [
            {"item_key": key, "status": status, "conclusion": conclusion,
             "evidence_refs": ["material:declared_fields"], "missing_evidence": missing}
            for key, status, conclusion, missing in (
                ("attachment_payment_evidence", "uncertain", gap, attachment_missing),
                ("invoice_linkage", "pass", "发票已关联。", []),
                ("budget", "pass", "当前阶段按既定策略暂不校验部门年度预算。", []),
                ("reimbursable_minimum", amount_status, amount_conclusion, amount_missing),
                ("payment_company_consistency", "pass", "报销公司、支付公司、购买方三者一致，应选择示例有限公司支付。", []),
            )
        ],
    }
    prompts = _mock_final_output(monkeypatch, json.dumps(recorded_decision))
    reviewed = codex_review_worker.review({
        "project_root": str(project_root), "action_file": str(action_file), "timeout_seconds": 60,
    })
    assert reviewed["status"] == "reviewed"
    assert len(prompts) == 1
    assert "references/observation-contract.md" in prompts[0]
    assert "references/payment-screenshot-exemption.md" in prompts[0]
    # These are loaded-rule guards; actual model acceptance remains independent.
    evidence_rule = (project_root / references / "observation-contract.md").read_text()
    payment_rule = (project_root / references / "payment-screenshot-exemption.md").read_text()
    assert "付款事实已核实不等于费用对应关系已闭合" in evidence_rule
    assert "分别核对付款事实与费用对应关系" in payment_rule
    assert "不进入下方 OA 收款人豁免及人员材料分支" in payment_rule
    assert "OA 收款人与销售方同名也不能消除已存在的实际交易冲突" in payment_rule
    assert reviewed["decision"] == recorded_decision
    decision_file = tmp_path / "expense-relation-decision.json"
    json_artifact.write_json_atomic(decision_file, reviewed["decision"])
    closed = engine.complete_review(action_file, decision_file)
    assert closed["business_state"] == "needs_human"
    assert closed["write_state"] == "not_requested"
    remark = Path(closed["remark_file"]).read_text()
    assert gap in remark
    assert "⚠️ 1、附件完整性/支付证据：" in remark
    assert amount_conclusion in remark
    if amount_status == "uncertain":
        assert "⚠️ 4、金额孰低核销：" in remark
    else:
        assert "✅ 4、金额孰低核销：" in remark


@pytest.mark.parametrize("amount_status,overall,conclusion", [
    ("pass", "pass", "按本轮财务审核金额与发票孰低，整单核销100元。"),
    ("fail", "needs_human", "本轮财务审核100元，可验证实付90元，核销上限90元。"),
])
def test_general_review_keeps_financial_basis_and_optional_payment_scope(
    tmp_path: Path, monkeypatch, amount_status: str, overall: str,
    conclusion: str,
) -> None:
    from tests.finance_preaudit.test_flow_engine_general_expense import _material

    project_root = tmp_path / "project"
    rule_path = Path(".agents/skills/oa-finance-preaudit/references/flows/general-expense.md")
    rule_file = project_root / rule_path
    rule_file.parent.mkdir(parents=True)
    rule_file.write_text((Path(__file__).resolve().parents[2] / rule_path).read_text())

    def collect_material(request_id, _):
        material = _material(request_id)
        material["declared_fields"].extend([
            {"path": "detail[0].row[0].field[0]", "name": "预算科目", "code": "yskm",
             "read_status": "present", "value": "办公费-邮寄费"},
            {"path": "detail[0].row[0].field[1]", "name": "财务审核金额", "code": "cwshje",
             "read_status": "present", "value": "100"},
            {"path": "main.field[0]", "name": "原始申请金额", "code": "old_request_amount",
             "read_status": "present", "value": "120"},
        ])
        return material

    engine = flow_engine.FlowEngine(
        root=project_root / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda key: [{
            "request_id": "synthetic-financial-basis", "workflow_key": key,
            "received_at": "2026-10-10T09:00:00+08:00",
        }],
        collect_material=collect_material,
    )
    action = engine.next_action("general_expense", mode="preview")
    recorded_decision = {
        "schema_version": 1, "kind": "preaudit_review_decision",
        "action_token": action["action_token"], "workflow_key": "general_expense",
        "rule_version": "general_expense:1", "overall": overall,
        "items": [
            {"item_key": key, "status": status, "conclusion": text,
             "evidence_refs": ["material:declared_fields"], "missing_evidence": gaps}
            for key, status, text, gaps in (
                ("attachment_payment_evidence", "pass", "邮寄费发票与运单符合。", []),
                ("invoice_linkage", "pass", "发票已关联。", []),
                ("budget", "pass", "当前阶段按既定策略暂不校验部门年度预算。", []),
                ("reimbursable_minimum", amount_status, conclusion, []),
                ("payment_company_consistency", "pass", "报销公司、支付公司、购买方三者一致，应选择示例有限公司支付。", []),
            )
        ],
    }
    prompts = _mock_final_output(monkeypatch, json.dumps(recorded_decision))
    action_file = Path(action["action_file"])
    reviewed = codex_review_worker.review({
        "project_root": str(project_root), "action_file": str(action_file), "timeout_seconds": 60,
    })
    assert reviewed["status"] == "reviewed"
    assert len(prompts) == 1
    assert "references/flows/general-expense.md" in prompts[0]
    # Rule loading guard; the real model verifies the financial interpretation.
    rule = rule_file.read_text()
    assert "旧纸质申请或原始申请金额不替代本轮财务审核金额" in rule
    assert "不因可选截图覆盖范围未知而新增付款证明义务" in rule
    assert "不能把已确认的低付差额降为仅 `⚠️`" in rule
    assert reviewed["decision"] == recorded_decision
    decision_file = tmp_path / "financial-basis-decision.json"
    json_artifact.write_json_atomic(decision_file, reviewed["decision"])
    closed = engine.complete_review(action_file, decision_file)
    assert closed["business_state"] == overall
    assert closed["write_state"] == "not_requested"
    assert conclusion in Path(closed["remark_file"]).read_text()


def test_small_marketing_v2_worker_uses_six_item_requirements(
    tmp_path: Path,
) -> None:
    _task_payload, action_file = _small_marketing_v2_task(
        tmp_path,
        budget_account="市场活动费",
    )
    item_keys = _SMALL_MARKETING_V2_ITEM_KEYS

    schema = codex_review_worker._decision_schema(action_file)
    prompt = codex_review_worker._prompt(action_file)

    assert schema["properties"]["rule_version"] == {
        "type": "string",
        "const": "small_marketing_fee:2",
    }
    items_schema = schema["properties"]["items"]
    assert items_schema["type"] == "object"
    assert items_schema["additionalProperties"] is False
    assert items_schema["required"] == item_keys
    assert list(items_schema["properties"]) == item_keys
    attachment_schema = items_schema["properties"]["attachment_completeness"]
    assert attachment_schema["properties"]["conclusion"]["pattern"] == (
        "^(?:.*支付截图.*发票.*|.*发票.*支付截图.*)$"
    )
    assert "item_key" not in attachment_schema["properties"]
    assert "item_key" not in attachment_schema["required"]
    assert "小额市场费生产预审" in prompt
    assert "small-marketing-fee.md" in prompt
    assert "六项必须逐项" in prompt
    assert "逐字同时包含“支付截图”和“发票”" in prompt
    assert "不得使用“支付凭证”等近义词替代" in prompt
    assert "支付截图和发票" in prompt
    assert "三者一致，应选择当前实际公司全称支付" in prompt
    assert (
        "budget_account → attachment_completeness → cost_bearer → "
        "sap_customer_code_match → reimbursable_minimum → "
        "payment_company_consistency"
    ) in prompt
    assert "不得重复、遗漏或调换顺序" in prompt
    assert "`items` 必须是以上六个规范键组成的对象" in prompt
    assert "各键的值不包含 `item_key`" in prompt
    assert "转换为 FlowEngine 决定数组" in prompt
    assert "符合 flow-engine-contract“结构化审核决定”" not in prompt
    assert "不得增加支付公司一致项" not in prompt


def test_small_marketing_v2_special_subject_keeps_general_item_schema(
    tmp_path: Path,
) -> None:
    _task_payload, action_file = _small_marketing_v2_task(
        tmp_path,
        budget_account="配送费-自提/租车费",
    )

    schema = codex_review_worker._decision_schema(action_file)

    item_schemas = schema["properties"]["items"]["properties"]
    assert list(item_schemas) == [
        "budget_account",
        "attachment_completeness",
        "cost_bearer",
        "sap_customer_code_match",
        "reimbursable_minimum",
        "payment_company_consistency",
    ]
    assert "pattern" not in item_schemas["attachment_completeness"]["properties"][
        "conclusion"
    ]


def test_small_marketing_v2_normalizes_keyed_items_in_frozen_order(
    tmp_path: Path,
) -> None:
    _task_payload, action_file = _small_marketing_v2_task(
        tmp_path,
        budget_account="市场活动费",
    )
    item_keys = _SMALL_MARKETING_V2_ITEM_KEYS
    keyed_items = {
        item_key: {
            "status": "uncertain",
            "conclusion": item_key,
            "evidence_refs": [],
            "missing_evidence": ["待确认"],
        }
        for item_key in reversed(item_keys)
    }

    normalized = codex_review_worker._normalize_decision(
        action_file,
        {"items": keyed_items},
    )

    assert [item["item_key"] for item in normalized["items"]] == item_keys


@pytest.mark.parametrize(
    "item_keys",
    [
        ["renamed", *_SMALL_MARKETING_V2_ITEM_KEYS[1:]],
        [*_SMALL_MARKETING_V2_ITEM_KEYS, "extra"],
        list(reversed(_SMALL_MARKETING_V2_ITEM_KEYS)),
        [
            _SMALL_MARKETING_V2_ITEM_KEYS[1],
            *_SMALL_MARKETING_V2_ITEM_KEYS[1:],
        ],
        _SMALL_MARKETING_V2_ITEM_KEYS[:-1],
    ],
    ids=["renamed", "extra", "reordered", "duplicate", "missing"],
)
def test_small_marketing_v2_rejects_noncanonical_packet_before_review(
    tmp_path: Path,
    item_keys: list[str],
) -> None:
    task, action_file = _small_marketing_v2_task(
        tmp_path,
        budget_account="市场活动费",
    )
    packet_file = action_file.with_name("review-packet.json")
    packet = json.loads(packet_file.read_text(encoding="utf-8"))
    json_artifact.write_json_atomic(
        packet_file,
        {**packet, "items": [{"item_key": item_key} for item_key in item_keys]},
    )

    result = codex_review_worker.review(
        task,
        executor=lambda *_args: pytest.fail("executor must not start"),
    )

    assert result["status"] == "failed"
    assert result["write_state"] == "not_requested"
    assert result["process_attempts"] == 0
    assert result["error_code"] == "codex_review_task_invalid"


def test_small_marketing_v2_rejects_changed_frozen_material_before_review(
    tmp_path: Path,
) -> None:
    task, action_file = _small_marketing_v2_task(
        tmp_path,
        budget_account="市场活动费",
    )
    json_artifact.write_json_atomic(
        action_file.with_name("material.json"),
        {"declared_fields": {"budget_account": "changed"}},
    )

    result = codex_review_worker.review(
        task,
        executor=lambda *_args: pytest.fail("executor must not start"),
    )

    assert result["status"] == "failed"
    assert result["write_state"] == "not_requested"
    assert result["process_attempts"] == 0
    assert result["error_code"] == "codex_review_task_invalid"


@pytest.mark.parametrize(
    "tamper",
    ["item_metadata", "packet_extra", "action_packet_path", "state_token"],
)
def test_small_marketing_v2_reuses_full_frozen_attempt_validation(
    tmp_path: Path,
    tamper: str,
) -> None:
    task, action_file = _small_marketing_v2_task(
        tmp_path,
        budget_account="市场活动费",
    )
    packet_file = action_file.with_name("review-packet.json")
    state_file = action_file.with_name("state.json")
    if tamper in {"item_metadata", "packet_extra"}:
        packet = json.loads(packet_file.read_text(encoding="utf-8"))
        if tamper == "item_metadata":
            packet["items"][0]["name"] = "changed"
        else:
            packet["unexpected"] = "changed"
        json_artifact.write_json_atomic(packet_file, packet)
    elif tamper == "action_packet_path":
        action = attempt_state.read_action(action_file)
        action["review_packet_file"] = str(action_file.with_name("other.json"))
        json_artifact.write_json_atomic(action_file, action)
    else:
        legacy = attempt_state.read_action(action_file)
        json_artifact.write_json_atomic(action_file, legacy)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state.pop("action_reference_sha256")
        state["action_token"] = "changed"
        json_artifact.write_json_atomic(state_file, state)

    result = codex_review_worker.review(
        task,
        executor=lambda *_args: pytest.fail("executor must not start"),
    )

    assert result["status"] == "failed"
    assert result["write_state"] == "not_requested"
    assert result["process_attempts"] == 0
    assert result["error_code"] == "codex_review_task_invalid"


def test_small_marketing_v1_worker_keeps_original_five_items(
    tmp_path: Path,
) -> None:
    _task_payload, action_file = _task(
        tmp_path,
        workflow_key="small_marketing_fee",
    )

    prompt = codex_review_worker._prompt(action_file)

    assert "冻结审核包为 V1" in prompt
    assert "只按包内原五项" in prompt
    assert "不得增加支付公司一致项" in prompt
    assert "六项必须逐项" not in prompt


def test_schema_uses_frozen_packet_rule_version(tmp_path: Path) -> None:
    _task_payload, action_file = _task(
        tmp_path,
        workflow_key="business_entertainment",
    )
    packet_file = action_file.with_name("review-packet.json")
    packet = json.loads(packet_file.read_text(encoding="utf-8"))
    json_artifact.write_json_atomic(
        packet_file,
        {**packet, "rule_version": "business_entertainment:2"},
    )

    schema = codex_review_worker._decision_schema(action_file)

    assert schema["properties"]["rule_version"] == {
        "type": "string",
        "const": "business_entertainment:2",
    }


def test_worker_rejects_non_object_frozen_packet(tmp_path: Path) -> None:
    task, action_file = _task(tmp_path)
    json_artifact.write_json_atomic(
        action_file.with_name("review-packet.json"),
        ["not-an-object"],
    )

    result = codex_review_worker.review(
        task,
        executor=lambda *_args: pytest.fail("executor must not start"),
    )

    assert result["status"] == "failed"
    assert result["write_state"] == "not_requested"
    assert result["error_code"] == "codex_review_task_invalid"


def test_codex_command_pins_deepseek_high_and_json_output(tmp_path: Path) -> None:
    command = codex_review_worker._codex_command(
        tmp_path, tmp_path / "schema.json", tmp_path / "decision.json",
    )
    assert command[command.index("--model") + 1] == "deepseek-flash"
    assert 'model_reasoning_effort="high"' in command
    assert 'model_provider="oa_deepseek"' in command
    assert 'service_tier="default"' in command
    assert "--json" in command and "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "memories.use_memories=false" in command
    assert "memories.generate_memories=false" in command
    assert 'model_providers.oa_deepseek.env_key="OA_REVIEW_API_KEY"' in command
    assert 'model_providers.oa_deepseek.requires_openai_auth=false' in command
    assert 'model_catalog_json=' + json.dumps(str(tmp_path / "config/deepseek-models.json")) in command
    assert not any("model_context_window" in argument for argument in command)
    import tomllib
    profile = next(argument for argument in command if argument.startswith("permissions.oa-reviewer="))
    permissions = tomllib.loads(profile)["permissions"]["oa-reviewer"]
    assert permissions["extends"] == ":read-only"
    assert permissions["filesystem"][str(tmp_path)] == "read"
    assert "deny" in permissions["filesystem"].values()
    assert "write" not in permissions["filesystem"].values()
    assert 'default_permissions="oa-reviewer"' in command
    assert 'history.persistence="none"' in command
    assert 'approval_policy="never"' in command


def test_cli_consumer_normalizes_small_marketing_transport_schema(tmp_path, monkeypatch):
    task, action_file = _small_marketing_v2_task(tmp_path, budget_account="市场活动费")
    _real_final_output(monkeypatch, json.dumps(_small_marketing_v2_decision()))

    result = codex_review_worker.review(task)

    assert result["status"] == "reviewed"
    assert [item["item_key"] for item in result["decision"]["items"]] == _SMALL_MARKETING_V2_ITEM_KEYS
    assert result["decision"]["action_token"] == attempt_state.read_action(action_file)["action_token"]
    decision_file = tmp_path / "consumer-decision.json"
    json_artifact.write_json_atomic(decision_file, result["decision"])
    engine = flow_engine.FlowEngine(root=action_file.parents[4],
                                   discover_candidates=lambda _: pytest.fail("preview must use frozen inputs"),
                                   collect_material=lambda *_: pytest.fail("preview must use frozen inputs"))
    closed = engine.complete_review(action_file, decision_file)
    assert closed["business_state"] == "needs_human" and closed["write_state"] == "not_requested"


def test_prompt_exposes_flow_engine_fallback_routing_for_legacy_packet(
    tmp_path: Path,
) -> None:
    _task_payload, action_file = _task(tmp_path)
    json_artifact.write_json_atomic(
        action_file.with_name("review-packet.json"),
        {
            "schema_version": 1,
            "kind": "preaudit_review_packet",
            "workflow_key": "travel_expense",
        },
    )
    json_artifact.write_json_atomic(
        action_file.with_name("material.json"),
        {
            "prepared": {
                "images": [
                    {"parent_resource_id": "resource-a"},
                    {"parent_resource_id": "resource-a"},
                ]
            }
        },
    )

    prompt = codex_review_worker._prompt(action_file)

    assert '"strategy":"full_page_fallback"' in prompt
    assert '"review_page_count":2' in prompt
    assert "material:prepared.images.resource-a.1" in prompt
    assert "material:prepared.images.resource-a.2" in prompt
    assert "逐项覆盖以下宿主 fallback 路由" in prompt
    assert '"strategy":"full_page_fallback"' in (
        codex_review_worker._read_only_prompt(action_file)
    )


def test_prompt_references_frozen_routing_without_copying_it_twice(
    tmp_path: Path,
) -> None:
    _task_payload, action_file = _task(tmp_path)
    packet_file = action_file.with_name("review-packet.json")
    material_file = action_file.with_name("material.json")
    json_artifact.write_json_atomic(
        material_file,
        {"prepared": {"images": [{"parent_resource_id": "resource-a"}]}},
    )
    json_artifact.write_json_atomic(
        packet_file,
        {
            "page_routing": {
                "strategy": "exact_page_duplicate_only",
                "review_page_refs": ["packet-owned-ref"],
            }
        },
    )

    prompt = codex_review_worker._prompt(action_file)
    assert "full_page_fallback" not in prompt
    assert '"strategy":"exact_page_duplicate_only"' not in prompt
    assert '"review_page_refs":["packet-owned-ref"]' not in prompt
    assert "直接读取冻结审核包中的 `page_routing`" in prompt
    assert "只表示逐项覆盖全部页面，不表示这些页面构成证据缺口" in prompt
    assert "除页面引用外还必须写明具体缺失事实或关系" in prompt
    assert '"review_page_refs":["packet-owned-ref"]' not in (
        codex_review_worker._read_only_prompt(action_file)
    )
    assert "只表示逐项覆盖全部页面，不表示这些页面构成证据缺口" in (
        codex_review_worker._read_only_prompt(action_file)
    )

    packet_file.write_text("not-json", encoding="utf-8")
    assert "full_page_fallback" not in codex_review_worker._prompt(action_file)


def test_codex_start_blocks_sigterm_until_process_group_is_registered(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OA_REVIEW_API_KEY", "unit-test-key")
    task, action_file = _task(tmp_path)
    calls: list[tuple[str, object]] = []

    def mask(how: object, values: object) -> set[signal.Signals]:
        calls.append(("mask", how))
        return set()

    def install(signum: int, handler: object) -> object:
        calls.append(("signal", signum))
        return signal.SIG_DFL

    class Process:
        pid = 12345
        returncode = 1
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")

        def communicate(self, *, input: str, timeout: int) -> None:
            del input, timeout
            calls.append(("communicate", self.pid))

        def poll(self) -> int:
            return self.returncode

    def popen(*_args, **_kwargs) -> Process:
        calls.append(("popen", "registered"))
        return Process()

    monkeypatch.setattr(codex_review_worker.signal, "pthread_sigmask", mask)
    monkeypatch.setattr(codex_review_worker.signal, "signal", install)
    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", popen)

    result = codex_review_worker._execute_codex(
        Path(task["project_root"]),
        action_file,
        60,
    )

    assert result.error_code == "codex_process_failed"
    assert [name for name, _value in calls[:5]] == [
        "mask",
        "signal",
        "popen",
        "mask",
        "communicate",
    ]
    assert calls[0][1] == signal.SIG_BLOCK
    assert calls[3][1] == signal.SIG_SETMASK


def test_codex_process_receives_only_reviewer_runtime_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task, action_file = _task(tmp_path)
    captured_environment: dict[str, str] = {}
    monkeypatch.setenv("OA_APP_SECRET", "must-not-reach-reviewer")
    monkeypatch.setenv("OA_DB_PASSWORD", "must-not-reach-reviewer")
    monkeypatch.setenv("OA_BASE_URL", "must-not-reach-reviewer")
    monkeypatch.setenv("OA_UID", "must-not-reach-reviewer")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-reach-reviewer")
    monkeypatch.setenv("OA_REVIEW_API_KEY", "deepseek-reviewer-key")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-reviewer")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-reach-reviewer")
    inherited_codex_home = tmp_path / "user-codex-home"
    inherited_codex_home.mkdir()
    (inherited_codex_home / "auth.json").write_text("user-login")
    monkeypatch.setenv("CODEX_HOME", str(inherited_codex_home))
    monkeypatch.setenv("OCX_SHIM_BYPASS", "0")

    class Process:
        pid = 12345
        returncode = 1
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")

        def communicate(self, *, input: str, timeout: int) -> None:
            del input, timeout

        def poll(self) -> int:
            return self.returncode

    def popen(*_args, **kwargs) -> Process:
        captured_environment.update(kwargs["env"])
        assert kwargs["env"]["CODEX_HOME"] != str(inherited_codex_home)
        assert not (Path(kwargs["env"]["CODEX_HOME"]) / "auth.json").exists()
        return Process()

    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", popen)

    result = codex_review_worker._execute_codex(
        Path(task["project_root"]),
        action_file,
        60,
    )

    assert result.error_code == "codex_process_failed"
    assert captured_environment["HOME"]
    assert captured_environment["PATH"]
    assert captured_environment["OA_REVIEW_API_KEY"] == "deepseek-reviewer-key"
    assert captured_environment["CODEX_HOME"] != str(inherited_codex_home)
    assert "OPENAI_API_KEY" not in captured_environment
    assert "CODEX_API_KEY" not in captured_environment
    assert captured_environment["OCX_SHIM_BYPASS"] == "1"
    assert "OA_APP_SECRET" not in captured_environment
    assert "OA_DB_PASSWORD" not in captured_environment
    assert "OA_BASE_URL" not in captured_environment
    assert "OA_UID" not in captured_environment
    assert "UNRELATED_SECRET" not in captured_environment


def test_production_review_phase_returns_decision_without_completing_action(
    tmp_path: Path,
) -> None:
    task, action_file = _task(tmp_path)
    decision = {"kind": "preaudit_review_decision", "items": []}

    result = codex_review_worker.review(
        task,
        executor=lambda *_args: codex_review_worker.CodexExecution(
            error_code=None,
            token_usage={
                "status": "recorded",
                "input_tokens": 10,
                "cached_input_tokens": 5,
                "output_tokens": 2,
                "total_tokens": 12,
            },
            decision=decision,
        ),
    )

    assert result["status"] == "reviewed"
    assert result["write_state"] == "not_requested"
    assert result["decision"] == decision
    assert result["codex_events"] == {
        "status": "unknown",
        "attempts": 1,
        "recorded_attempts": 0,
        "unknown_attempts": 1,
        "counts": {},
    }
    assert attempt_state.read_action(action_file)["action"] == (
        "review_required"
    )
    assert not action_file.with_name("agent-timing.json").exists()
    assert not action_file.with_name("execution-receipt.json").exists()


def test_production_review_retains_first_failure_events_and_tokens(
    tmp_path: Path,
) -> None:
    task, _action_file = _task(tmp_path)
    executions = iter(
        [
            codex_review_worker.CodexExecution(
                error_code="codex_process_failed",
                token_usage={
                    "status": "recorded",
                    "input_tokens": 10,
                    "cached_input_tokens": 2,
                    "output_tokens": 1,
                    "total_tokens": 11,
                },
                event_counts={"error": 1, "turn.completed": 1},
                event_status="recorded",
            ),
            codex_review_worker.CodexExecution(
                error_code=None,
                token_usage={
                    "status": "recorded",
                    "input_tokens": 20,
                    "cached_input_tokens": 5,
                    "output_tokens": 3,
                    "total_tokens": 23,
                },
                decision={"overall": "pass"},
                event_counts={"turn.completed": 1},
                event_status="recorded",
            ),
        ]
    )

    result = codex_review_worker.review(
        task,
        executor=lambda *_args: next(executions),
    )

    assert result["status"] == "failed"
    assert result["process_attempts"] == 1
    assert result["token_usage"] == {
        "status": "recorded",
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 1,
        "total_tokens": 11,
    }
    assert result["codex_events"] == {
        "status": "recorded",
        "attempts": 1,
        "recorded_attempts": 1,
        "unknown_attempts": 0,
        "counts": {"error": 1, "turn.completed": 1},
    }


def test_turn_failed_review_stops_after_one_attempt(tmp_path: Path) -> None:
    task, action_file = _task(tmp_path)

    result = codex_review_worker.review(
        task,
        executor=lambda *_args: codex_review_worker.CodexExecution(
            error_code="codex_process_failed",
            token_usage={"status": "unknown"},
            event_counts={
                "error": 1,
                "thread.started": 1,
                "turn.failed": 1,
                "turn.started": 1,
            },
            event_status="recorded",
            process_state="exited",
            exit_code=1,
        ),
    )

    assert result["process_attempts"] == 1
    assert result["codex_events"]["counts"].get("turn.failed") == 1
    assert not runtime_control.stop_requested()
    assert codex_review_worker.is_bounded_prewrite_failure(result)
    assert action_file.with_name("review-failure.json").is_file()


@pytest.mark.parametrize("code,count", [("codex_process_failed", 1), ("codex_process_timeout", 1)])
def test_bounded_prewrite_failure_persists_bound_receipt(tmp_path: Path, code: str, count: int) -> None:
    task, action_file = _task(tmp_path)

    result = codex_review_worker.review(
        task,
        executor=lambda *_args: codex_review_worker.CodexExecution(
            error_code=code,
            token_usage={"status": "unknown"},
        ),
    )

    assert codex_review_worker.is_bounded_prewrite_failure(result)
    receipt = json.loads(
        action_file.with_name("review-failure.json").read_text(encoding="utf-8")
    )
    assert receipt == {
        "schema_version": 1,
        "kind": "preaudit_review_failure",
        "action_token": "test-action-token",
        "workflow_key": "travel_expense",
        "model": codex_review_worker.MODEL,
        "reasoning_effort": codex_review_worker.REASONING_EFFORT,
        "model_provider": review_failure_evidence.MODEL_PROVIDER,
        "requested_service_tier": "default",
        "process_attempts": count,
        "process_retries": count - 1,
        "failure_code": code,
        "attempt_error_codes": [code] * count,
    }


def test_payload_too_large_keeps_diagnostic_and_existing_failure_boundary(tmp_path: Path) -> None:
    task, action_file = _task(tmp_path)
    result = codex_review_worker.review(task, executor=lambda *_: codex_review_worker.CodexExecution(
        error_code="codex_process_failed", token_usage={"status": "unknown"},
        event_signals=("payload_too_large_signal",), process_state="exited", exit_code=1,
    ))
    assert result["process_attempts"] == 1
    assert result["write_state"] == "not_requested"
    assert result["codex_events"]["counts"]["payload_too_large_signal"] == 1
    assert codex_review_worker.is_bounded_prewrite_failure(result)
    assert action_file.with_name("review-failure.json").is_file()


def test_bounded_prewrite_failure_reuses_matching_receipt_after_stop(
    tmp_path: Path,
) -> None:
    task, action_file = _task(tmp_path)

    def fail(*_args) -> codex_review_worker.CodexExecution:
        return codex_review_worker.CodexExecution(
            error_code="codex_process_failed",
            token_usage={"status": "unknown"},
        )

    first = codex_review_worker.review(task, executor=fail)
    receipt_file = action_file.with_name("review-failure.json")
    first_receipt = receipt_file.read_bytes()
    runtime_control.request_stop()
    second = codex_review_worker.review(task, executor=fail)

    assert codex_review_worker.is_bounded_prewrite_failure(first)
    assert second["error_code"] == "stop_requested"
    assert second["process_attempts"] == 0
    assert receipt_file.read_bytes() == first_receipt


def test_bounded_prewrite_failure_rejects_conflicting_receipt(
    tmp_path: Path,
) -> None:
    task, action_file = _task(tmp_path)
    action_file.with_name("review-failure.json").write_text("{}", encoding="utf-8")

    result = codex_review_worker.review(
        task,
        executor=lambda *_args: codex_review_worker.CodexExecution(
            error_code="codex_process_failed",
            token_usage={"status": "unknown"},
        ),
    )

    assert result["error_code"] == "codex_review_failure_evidence_invalid"


def test_bounded_prewrite_failure_rejects_incomplete_worker_contract() -> None:
    assert not codex_review_worker.is_bounded_prewrite_failure(
        {
            "status": "failed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "error_code": "codex_process_failed",
            "process_attempts": 2,
            "process_retries": 1,
        }
    )


def test_parent_completion_persists_timing_and_bound_receipt(
    tmp_path: Path,
) -> None:
    _task_value, action_file = _task(tmp_path)
    decision = {"kind": "preaudit_review_decision", "items": []}

    def complete(current_action: Path, current_decision: dict) -> str | None:
        assert current_decision == decision
        receipt_file = current_action.with_name("execution-receipt.json")
        json_artifact.write_json_atomic(
            receipt_file,
            {
                "write": True,
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": True,
            },
        )
        action = attempt_state.read_action(current_action)
        json_artifact.write_json_atomic(
            current_action,
            {
                **action,
                "action": "closed",
                "execution_state": "closed",
                "business_state": "pass",
                "write_state": "submitted",
                "receipt_file": str(receipt_file),
            },
        )
        return None

    result = codex_review_worker.complete_review_result(
        action_file,
        {
            "status": "reviewed",
            "model": codex_review_worker.MODEL, "reasoning_effort": codex_review_worker.REASONING_EFFORT,
            "model_provider": review_failure_evidence.MODEL_PROVIDER, "requested_service_tier": "default",
            "process_attempts": 1,
            "agent_wall_seconds": 12,
            "token_usage": {"status": "unknown"},
            "codex_events": {
                "status": "recorded",
                "attempts": 1,
                "recorded_attempts": 1,
                "unknown_attempts": 0,
                "counts": {"turn.completed": 1},
            },
            "decision": decision,
        },
        completer=complete,
        request_stop=lambda: None,
    )

    assert result["status"] == "completed"
    assert result["write_state"] == "submitted"
    assert result["codex_events"]["counts"] == {"turn.completed": 1}
    timing = json.loads(
        action_file.with_name("agent-timing.json").read_text(encoding="utf-8")
    )
    assert timing["wall_seconds"] == 12


def test_parent_completion_can_defer_first_invalid_decision_stop(
    tmp_path: Path,
) -> None:
    _task_value, action_file = _task(tmp_path)
    stopped: list[bool] = []

    result = codex_review_worker.complete_review_result(
        action_file,
        {
            "status": "reviewed",
            "model": codex_review_worker.MODEL, "reasoning_effort": codex_review_worker.REASONING_EFFORT,
            "model_provider": review_failure_evidence.MODEL_PROVIDER, "requested_service_tier": "default",
            "process_attempts": 1,
            "agent_wall_seconds": 12,
            "token_usage": {"status": "unknown"},
            "codex_events": {
                "status": "recorded",
                "attempts": 1,
                "recorded_attempts": 1,
                "unknown_attempts": 0,
                "counts": {"turn.completed": 1},
            },
            "decision": {"kind": "preaudit_review_decision", "items": []},
        },
        completer=lambda _action, _decision: "codex_decision_invalid",
        request_stop=lambda: stopped.append(True),
        defer_invalid_stop=True,
    )

    assert result["status"] == "retry_required"
    assert result["error_code"] == "codex_decision_invalid"
    assert result["write_state"] == "not_requested"
    assert stopped == []
    assert not action_file.with_name("execution-receipt.json").exists()


@pytest.mark.parametrize(
    "failure_code",
    (
        "writer_submit_rejected",
        "writer_transport_unknown",
        "writer_postcheck_timeout",
        "writer_pending_conflict",
        "writer_preflight_failed",
        "submission_target_changed",
        "writer_result_unknown",
    ),
)
def test_host_completion_preserves_allowlisted_writer_failure(
    tmp_path: Path,
    monkeypatch,
    failure_code: str,
) -> None:
    class Engine:
        def complete_review(self, _action_file: Path, _decision_file: Path) -> dict:
            return {
                "action": "needs_admin",
                "execution_state": "needs_admin",
                "write_state": "unknown",
                "reason": failure_code,
            }

    monkeypatch.setattr(
        "apps.oa_finance_preaudit.cli._live_flow_engine",
        lambda *, controlled: Engine(),
    )

    assert (
        codex_review_worker._complete_decision(
            tmp_path / "action.json",
            {"kind": "preaudit_review_decision"},
        )
        == failure_code
    )


def test_host_completion_rejects_unrecognized_failure_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Engine:
        def complete_review(self, _action_file: Path, _decision_file: Path) -> dict:
            return {
                "action": "needs_admin",
                "execution_state": "needs_admin",
                "write_state": "unknown",
                "reason": "unrecognized_internal_detail",
            }

    monkeypatch.setattr(
        "apps.oa_finance_preaudit.cli._live_flow_engine",
        lambda *, controlled: Engine(),
    )

    assert (
        codex_review_worker._complete_decision(
            tmp_path / "action.json",
            {"kind": "preaudit_review_decision"},
        )
        == "codex_decision_submit_incomplete"
    )


@pytest.mark.parametrize(
    ("failure_code", "expected_write_state"),
    (
        ("writer_submit_rejected", "not_requested"),
        ("writer_transport_unknown", "unknown"),
        ("writer_postcheck_timeout", "unknown"),
        ("writer_pending_conflict", "unknown"),
        ("writer_preflight_failed", "not_requested"),
        ("submission_target_changed", "not_requested"),
        ("writer_result_unknown", "unknown"),
    ),
)
def test_parent_completion_reports_writer_failure_state(
    tmp_path: Path,
    failure_code: str,
    expected_write_state: str,
) -> None:
    _task_value, action_file = _task(tmp_path)
    stopped: list[bool] = []

    result = codex_review_worker.complete_review_result(
        action_file,
        {
            "status": "reviewed",
            "model": codex_review_worker.MODEL, "reasoning_effort": codex_review_worker.REASONING_EFFORT,
            "model_provider": review_failure_evidence.MODEL_PROVIDER, "requested_service_tier": "default",
            "process_attempts": 1,
            "agent_wall_seconds": 12,
            "token_usage": {"status": "unknown"},
            "codex_events": {
                "status": "recorded",
                "attempts": 1,
                "recorded_attempts": 1,
                "unknown_attempts": 0,
                "counts": {"turn.completed": 1},
            },
            "decision": {"kind": "preaudit_review_decision", "items": []},
        },
        completer=lambda _action, _decision: failure_code,
        request_stop=lambda: stopped.append(True),
    )

    assert result["status"] == "failed"
    assert result["error_code"] == failure_code
    assert result["write_state"] == expected_write_state
    assert stopped == [True]


def test_errored_stream_without_terminal_event_is_recorded() -> None:
    """上游报错后进程直接退出(没有 turn.failed / turn.completed):
    失败已由 error 事件说明,同样算已记录,避免直接隔离。"""
    stream = io.StringIO(
        '{"type":"thread.started","thread_id":"t"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"error","message":"Selected model is at capacity. '
        'Please try a different model."}\n'
    )

    counts = codex_review_worker._parse_event_counts(stream)

    assert codex_review_worker._event_terminal_count(counts) == 0
    assert codex_review_worker._event_status(stream, counts) == "recorded"
    assert codex_review_worker._valid_events(
        {
            "status": "recorded",
            "attempts": 2,
            "recorded_attempts": 2,
            "unknown_attempts": 0,
            "counts": {"error": 2, "thread.started": 2, "turn.started": 2},
        },
        attempts=2,
    )


def test_truncated_stream_without_error_event_stays_unknown() -> None:
    stream = io.StringIO(
        '{"type":"thread.started","thread_id":"t"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.started","item":{"id":"i","type":"reasoning"}}\n'
    )

    counts = codex_review_worker._parse_event_counts(stream)

    assert codex_review_worker._event_status(stream, counts) == "unknown"
    assert not codex_review_worker._valid_events(
        {
            "status": "recorded",
            "attempts": 1,
            "recorded_attempts": 1,
            "unknown_attempts": 0,
            "counts": counts,
        },
        attempts=1,
    )


def test_successful_stream_still_requires_a_terminal_event_per_attempt() -> None:
    counts = {"error": 1, "turn.completed": 2}

    assert codex_review_worker._valid_events(
        {
            "status": "recorded",
            "attempts": 2,
            "recorded_attempts": 2,
            "unknown_attempts": 0,
            "counts": counts,
        },
        attempts=2,
    )
    assert not codex_review_worker._valid_events(
        {
            "status": "recorded",
            "attempts": 2,
            "recorded_attempts": 2,
            "unknown_attempts": 0,
            "counts": {"error": 0, "turn.completed": 1},
        },
        attempts=2,
    )


def test_capacity_signal_is_recorded_without_the_message_text() -> None:
    diagnostics = codex_review_worker.process_diagnostics

    summary = diagnostics.stderr_summary(
        io.StringIO("Selected model is at capacity. Please try a different model.")
    )

    assert summary["stderr_signals"] == ["capacity_signal"]
    assert "capacity" not in json.dumps(summary) .replace("capacity_signal", "")


def test_turn_failed_stream_is_recorded_and_bounded_prewrite_failure() -> None:
    """上游以 turn.failed 结束(如 Selected model is at capacity)时:
    事件流仍视为完整,失败属于有界写前失败,因此进入跨轮重试而不是立即隔离。
    """
    stream = io.StringIO(
        '{"type":"thread.started","thread_id":"t"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"error","message":"Selected model is at capacity. '
        'Please try a different model."}\n'
        '{"type":"turn.failed","error":{"message":"Selected model is at '
        'capacity. Please try a different model."}}\n'
    )

    counts = codex_review_worker._parse_event_counts(stream)
    assert counts == {"error": 1, "thread.started": 1, "turn.failed": 1, "turn.started": 1}
    assert codex_review_worker._event_status(stream, counts) == "recorded"
    assert codex_review_worker._event_terminal_count(counts) == 1
    assert codex_review_worker._valid_events(
        {
            "status": "recorded",
            "attempts": 1,
            "recorded_attempts": 1,
            "unknown_attempts": 0,
            "counts": counts,
        },
        attempts=1,
    )


def test_turn_failed_without_terminal_event_stays_unknown() -> None:
    stream = io.StringIO(
        '{"type":"thread.started","thread_id":"t"}\n'
        '{"type":"turn.started"}\n'
    )

    counts = codex_review_worker._parse_event_counts(stream)

    assert codex_review_worker._event_status(stream, counts) == "unknown"
    assert codex_review_worker._event_terminal_count(counts) == 0


def test_usage_parser_reads_only_turn_completed_usage() -> None:
    stream = io.StringIO(
        '\n'.join(
            [
                '{"type":"item.completed","usage":{"input_tokens":999}}',
                '{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":20,"output_tokens":30}}',
                "not-json",
            ]
        )
    )

    assert codex_review_worker._parse_token_usage(stream) == {
        "status": "recorded",
        "input_tokens": 120,
        "cached_input_tokens": 20,
        "output_tokens": 30,
        "total_tokens": 150,
    }


def test_event_status_rejects_malformed_jsonl_and_keeps_classified_errors() -> None:
    complete = io.StringIO('{"type":"turn.completed"}\n')
    error_only = io.StringIO('{"type":"error"}\n')
    truncated = io.StringIO('{"type":"turn.started"}\n')
    malformed = io.StringIO('{"type":"turn.completed"}\nnot-json\n')

    assert codex_review_worker._event_status(
        complete,
        {"turn.completed": 1},
    ) == "recorded"
    # error 事件已经说明失败,算已记录(不再直接隔离);没有 error 又没有终态事件的
    # 截断流仍是 unknown。
    assert codex_review_worker._event_status(error_only, {"error": 1}) == "recorded"
    assert codex_review_worker._event_status(truncated, {"turn.started": 1}) == "unknown"
    assert codex_review_worker._event_status(
        malformed,
        {"turn.completed": 1},
    ) == "unknown"


def test_event_parser_sanitizes_unknown_event_types() -> None:
    sensitive_type = "request-123-/internal/path"
    stream = io.StringIO(
        f'{{"type":"{sensitive_type}"}}\n{{"type":"turn.completed"}}\n'
    )

    counts = codex_review_worker._parse_event_counts(stream)

    assert counts == {"other": 1, "turn.completed": 1}
    assert sensitive_type not in repr(counts)
    assert codex_review_worker._event_status(stream, counts) == "unknown"


def test_parent_invalid_review_preserves_known_attempts_as_unknown_events(
    tmp_path: Path,
) -> None:
    _task_value, action_file = _task(tmp_path)
    stopped: list[bool] = []

    result = codex_review_worker.complete_review_result(
        action_file,
        {
            "status": "reviewed",
            "model": codex_review_worker.MODEL, "reasoning_effort": codex_review_worker.REASONING_EFFORT,
            "model_provider": review_failure_evidence.MODEL_PROVIDER, "requested_service_tier": "default",
            "process_attempts": 2,
            "agent_wall_seconds": 12,
            "token_usage": {"status": "unknown"},
            "codex_events": {"status": "unknown"},
            "decision": {"overall": "pass"},
        },
        request_stop=lambda: stopped.append(True),
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "codex_review_result_invalid"
    assert result["process_attempts"] == 2
    assert result["process_retries"] == 1
    assert result["codex_events"] == {
        "status": "unknown",
        "attempts": 2,
        "recorded_attempts": 0,
        "unknown_attempts": 2,
        "counts": {},
    }
    assert stopped == [True]


def test_event_contract_rejects_recorded_attempt_without_completed_event() -> None:
    assert not codex_review_worker._valid_events(
        {
            "status": "recorded",
            "attempts": 1,
            "recorded_attempts": 1,
            "unknown_attempts": 0,
            "counts": {},
        },
        attempts=1,
    )


def test_host_completion_does_not_add_a_second_writer_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "OA_FROZEN_WRITER_LOCK_FILE",
        str(tmp_path / "control" / "writer.lock"),
    )
    active = 0
    peak = 0
    guard = threading.Lock()

    class Engine:
        def complete_review(self, _action_file: Path, _decision_file: Path) -> dict:
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with guard:
                active -= 1
            return {
                "action": "closed",
                "execution_state": "closed",
                "write_state": "submitted",
            }

    monkeypatch.setattr(
        "apps.oa_finance_preaudit.cli._live_flow_engine",
        lambda *, controlled: Engine(),
    )
    results: list[str | None] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                codex_review_worker._complete_decision(
                    tmp_path / "action.json",
                    {"kind": "preaudit_review_decision"},
                )
            )
        )
        for _index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [None, None]
    assert peak == 2


def test_global_stop_request_is_visible_to_all_workers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_file = tmp_path / "control" / "stop.flag"
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_FILE", str(stop_file))

    runtime_control.request_stop()

    assert runtime_control.stop_requested() is True
    assert stop_file.read_text(encoding="utf-8") == "stop_requested\n"
    assert stop_file.stat().st_mode & 0o777 == 0o600


def test_global_stop_preserves_first_sanitized_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_file = tmp_path / "control" / "stop.flag"
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_FILE", str(stop_file))

    runtime_control.request_stop("production_probe_prepare_failed")
    runtime_control.request_stop("production_probe_renew_failed")

    assert runtime_control.stop_requested() is True
    assert runtime_control.stop_reason() == "production_probe_prepare_failed"
    assert (
        stop_file.read_text(encoding="utf-8")
        == "stop_requested:production_probe_prepare_failed\n"
    )


def test_cycle_stop_is_cleared_after_controller_reaps_children(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_file = tmp_path / "control" / "stop.flag"
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_FILE", str(stop_file))

    runtime_control.request_cycle_stop()

    assert runtime_control.stop_requested() is True
    assert runtime_control.stop_reason() == "controller_cycle_stop"
    assert runtime_control.clear_cycle_stop() is True
    assert runtime_control.stop_requested() is False


def test_cycle_stop_clear_preserves_manual_reconciliation_stop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_file = tmp_path / "control" / "stop.flag"
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_FILE", str(stop_file))

    runtime_control.request_stop("writer_result_unknown")

    assert runtime_control.clear_cycle_stop() is False
    assert runtime_control.stop_requested() is True
    assert runtime_control.stop_reason() == "writer_result_unknown"


def test_global_stop_rejects_unlisted_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_file = tmp_path / "control" / "stop.flag"
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_FILE", str(stop_file))

    with pytest.raises(ValueError, match="stop_reason_invalid"):
        runtime_control.request_stop("unexpected_reason")

    assert not stop_file.exists()


def test_global_stop_reason_rejects_tampered_or_symlinked_content(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_file = tmp_path / "control" / "stop.flag"
    stop_file.parent.mkdir(parents=True)
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_FILE", str(stop_file))
    stop_file.write_text(
        "stop_requested:production_probe_prepare_failed \n",
        encoding="ascii",
    )

    assert runtime_control.stop_reason() is None

    outside = tmp_path / "outside-stop.flag"
    outside.write_text(
        "stop_requested:production_probe_prepare_failed\n",
        encoding="ascii",
    )
    stop_file.unlink()
    stop_file.symlink_to(outside)

    assert runtime_control.stop_reason() is None


def test_read_only_worker_returns_structured_decision_without_completion(
    tmp_path: Path,
) -> None:
    task, _action_file = _task(tmp_path)
    decision = {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "overall": "needs_human",
    }

    result = codex_review_worker.run_read_only(
        task,
        executor=lambda *_args: codex_review_worker.CodexExecution(
            error_code=None,
            token_usage={
                "status": "recorded",
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 10,
                "total_tokens": 110,
            },
            decision=decision,
            process_state="exited",
            decision_state="valid_object",
            event_counts={"turn.completed": 1},
            event_status="recorded",
            exit_code=0,
        ),
    )

    assert result == {
        "schema_version": 1,
        "kind": "preaudit_read_only_review_result",
        "status": "completed",
        "business_state": "needs_human",
        "write_state": "not_requested",
        "error_code": None,
        "model": codex_review_worker.MODEL,
        "reasoning_effort": codex_review_worker.REASONING_EFFORT,
        "model_provider": review_failure_evidence.MODEL_PROVIDER,
        "requested_service_tier": "default",
        "process_attempts": 1,
        "process_retries": 0,
        "process_state": "exited",
        "decision_state": "valid_object",
        "codex_events": {
            "status": "recorded",
            "attempts": 1,
            "recorded_attempts": 1,
            "unknown_attempts": 0,
            "counts": {"turn.completed": 1},
        },
        "exit_code": 0,
        "token_usage": {
            "status": "recorded",
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 10,
            "total_tokens": 110,
        },
    }


def test_read_only_worker_classifies_zero_exit_without_decision() -> None:
    result = codex_review_worker.run_read_only(
        {
            "project_root": "/missing",
            "action_file": "/missing/action.json",
            "timeout_seconds": 900,
        },
    )

    assert result["status"] == "failed"
    assert result["write_state"] == "not_requested"
    assert result["error_code"] == "codex_review_task_invalid"


@pytest.mark.parametrize("workflow", list(codex_review_worker._FINANCE_RULES))
def test_output_schema_binds_current_action_token(tmp_path: Path, workflow: str) -> None:
    _, action_file = _task(tmp_path, workflow_key=workflow)
    schema = codex_review_worker._decision_schema(action_file)
    assert schema["properties"]["action_token"] == {
        "type": "string", "const": "test-action-token",
    }


def test_v2_output_schema_binds_real_frozen_token(tmp_path: Path) -> None:
    _, action_file = _small_marketing_v2_task(tmp_path, budget_account="市场费用")
    token = attempt_state.read_action(action_file)["action_token"]
    schema = codex_review_worker._decision_schema(action_file)
    assert schema["properties"]["action_token"] == {"type": "string", "const": token}
    wrong = {"action_token": "wrong-token"}
    assert codex_review_worker._normalize_decision(action_file, wrong)["action_token"] == "wrong-token"


@pytest.mark.parametrize("token", [None, "", 123, "different-token"])
def test_output_schema_rejects_invalid_or_unbound_token(tmp_path: Path, token) -> None:
    _, action_file = _task(tmp_path)
    action = attempt_state.read_action(action_file)
    action["action_token"] = token
    json_artifact.write_json_atomic(action_file, action)
    with pytest.raises(ValueError, match="codex_review_task_invalid"):
        codex_review_worker._decision_schema(action_file)


def test_read_only_schema_keeps_probe_token(tmp_path: Path) -> None:
    _, action_file = _task(tmp_path)
    schema = codex_review_worker._decision_schema(action_file, read_only=True)
    assert schema["properties"]["action_token"] == {"type": "string", "const": "capacity-probe"}


@pytest.mark.parametrize("read_only", [False, True])
def test_process_receives_bound_token_schema(tmp_path: Path, monkeypatch, read_only: bool) -> None:
    monkeypatch.setenv("OA_REVIEW_API_KEY", "unit-test-key")
    task, action_file = _task(tmp_path)
    seen = []

    class Process:
        pid = 12345
        returncode = 1
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")

        def communicate(self, *, input: str, timeout: int) -> None:
            expected = "capacity-probe" if read_only else "test-action-token"
            if read_only:
                assert expected in input

        def poll(self) -> int:
            return self.returncode

    def popen(command, **_kwargs):
        schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
        seen.append(schema["properties"]["action_token"])
        return Process()

    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", popen)
    executor = (codex_review_worker._execute_read_only_codex if read_only
                else codex_review_worker._execute_codex)
    executor(Path(task["project_root"]), action_file, 60)
    expected = "capacity-probe" if read_only else "test-action-token"
    assert seen == [{"type": "string", "const": expected}]


def test_invalid_bound_token_fails_before_process_start(tmp_path: Path, monkeypatch) -> None:
    task, action_file = _task(tmp_path)
    state_file = action_file.with_name("state.json")
    state = json.loads(state_file.read_text())
    state["action_token"] = "other-action"
    json_artifact.write_json_atomic(state_file, state)

    def unexpected_start(*_args, **_kwargs):
        pytest.fail("invalid token must not start a reviewer")

    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", unexpected_start)
    result = codex_review_worker.review(task)
    assert result["status"] == "failed"
    assert result["error_code"] == "codex_review_task_invalid"
    assert result["write_state"] == "not_requested"
    assert not action_file.with_name("review-decision.json").exists()


def test_review_prompt_keeps_required_business_values_inside_decision(tmp_path):
    _, action = _task(tmp_path, workflow_key="general_expense")
    prompt = codex_review_worker._prompt(action)
    assert "JSON 之外" in prompt
    assert "具体金额、实际公司全称" in prompt
    assert "不得输出密钥、凭据" in prompt
    assert "不添加 Markdown、解释、单号、人员、公司" not in prompt


def test_tpm_prompt_requires_exact_evidence_references(tmp_path):
    _, action = _task(tmp_path, workflow_key="tpm_small_marketing_fee")
    packet_file = action.with_name("review-packet.json")
    packet = json.loads(packet_file.read_text())
    packet["rule_version"] = "tpm_small_marketing_fee:4"
    json_artifact.write_json_atomic(packet_file, packet)
    prompt = codex_review_worker._prompt(action)
    assert "逐字使用 declared_fields 中的 path" in prompt
    assert "不使用视图行号、简写路径或范围表达式" in prompt
    assert "material.authority_facts[实际数组索引]" in prompt


def test_tpm_prompt_keeps_passing_conclusions_short_and_direct(tmp_path):
    _, action = _task(tmp_path, workflow_key="tpm_small_marketing_fee")
    packet_file = action.with_name("review-packet.json")
    packet = json.loads(packet_file.read_text())
    packet["rule_version"] = "tpm_small_marketing_fee:4"
    json_artifact.write_json_atomic(packet_file, packet)

    prompt = codex_review_worker._prompt(action)

    assert "通过项只写一句给审批人的结论" in prompt
    assert "不复述取证过程" in prompt
    assert "核销项直接写最终核销金额" in prompt
    assert "支付公司项先写实际报销公司、支付公司与发票购买方全称及比较结果" in prompt
    assert "每个审核项只写一句给审批人的结论" in prompt
    assert "输出前自检" in prompt


def test_reviewer_environment_isolates_deepseek_key_from_native_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OA_REVIEW_API_KEY", "must-not-leak")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_HOME", "/user/codex/auth")
    environment = codex_review_worker._reviewer_environment()
    assert "CODEX_HOME" not in environment
    assert environment["OA_REVIEW_API_KEY"] == "must-not-leak"
    assert not {"UNRELATED_SECRET", "OPENAI_API_KEY", "CODEX_API_KEY"} & environment.keys()
    assert environment["HOME"] and environment["PATH"]


def test_reviewer_environment_rejects_missing_deepseek_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OA_REVIEW_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    with pytest.raises(ValueError, match="codex_reviewer_api_key_missing"):
        codex_review_worker._reviewer_environment()


def test_legacy_openai_review_evidence_still_validates() -> None:
    legacy = review_failure_evidence.receipt_payload(
        action_token="legacy-token",
        workflow_key="travel_expense",
        failure_code="codex_process_failed",
        attempt_error_codes=["codex_process_failed", "codex_process_failed"],
    )
    legacy["model"] = "gpt-5.6-terra"
    legacy["reasoning_effort"] = "high"
    legacy.pop("model_provider")
    legacy.pop("requested_service_tier")

    assert review_failure_evidence.receipt_matches(
        legacy,
        action_token="legacy-token",
        workflow_key="travel_expense",
    )
    assert review_failure_evidence.model_matches("gpt-5.6-terra")
    assert review_failure_evidence.model_matches("deepseek-v4-flash")
    assert not review_failure_evidence.model_matches(None)

    tampered = {**legacy, "model": "unknown-model"}
    assert not review_failure_evidence.receipt_matches(
        tampered,
        action_token="legacy-token",
        workflow_key="travel_expense",
    )


def test_production_failure_round_uses_existing_controlled_engine(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import cli
    calls = []
    path = tmp_path / 'action.json'
    expected = {'status': 'recorded', 'write_state': 'not_requested'}
    class Engine:
        def record_review_failure_round(self, action_file, failure_code):
            calls.append((action_file, failure_code))
            return expected
    def live(*, controlled):
        assert controlled is True
        return Engine()
    monkeypatch.setattr(cli, '_live_flow_engine', live)
    assert codex_review_worker.record_production_failure_round(path, 'codex_decision_invalid') is expected
    assert calls == [(path, 'codex_decision_invalid')]


@pytest.mark.parametrize("model,effort,accepted", [
    ("deepseek-flash", "high", True), ("deepseek-flash", "medium", False),
    ("gpt-6.1-sol", "medium", True), ("gpt-6.1-sol", "high", False),
    ("gpt-5.6-terra", "high", True), ("gpt-5.6-terra", "medium", False),
    ("deepseek-v4-flash", "high", True), ("deepseek-v4-flash", "medium", False),
    ("deepseek-v4-pro", "high", True), ("unknown", "medium", False),
])
def test_failure_receipt_preserves_model_effort_contract(model, effort, accepted):
    receipt = review_failure_evidence.receipt_payload(
        action_token="token", workflow_key="travel_expense", failure_code="codex_process_failed",
        attempt_error_codes=["codex_process_failed", "codex_process_failed"],
    )
    receipt.update(model=model, reasoning_effort=effort)
    receipt.pop("model_provider")
    receipt.pop("requested_service_tier")
    receipt.update(review_failure_evidence.MODEL_SETTINGS.get(model, {}))
    original = json.dumps(receipt)
    assert review_failure_evidence.receipt_matches(receipt, action_token="token", workflow_key="travel_expense") is accepted
    assert json.dumps(receipt) == original


def test_current_failure_receipt_records_requested_provider_and_service_tier():
    receipt = review_failure_evidence.receipt_payload(
        action_token="token", workflow_key="travel_expense", failure_code="codex_process_failed",
        attempt_error_codes=["codex_process_failed", "codex_process_failed"],
    )
    assert receipt["model_provider"] == "oa_deepseek"
    assert receipt["requested_service_tier"] == "default"
    for field, value in (("model_provider", "openai"), ("requested_service_tier", "priority")):
        tampered = {**receipt, field: value}
        assert not review_failure_evidence.receipt_matches(tampered, action_token="token", workflow_key="travel_expense")


@pytest.mark.parametrize("model,effort", [("gpt-6.1-sol", "high"), ("deepseek-v4-flash", "medium"), ("gpt-6.1-sol", ["medium"]), ("gpt-6.1-sol", {"effort": "medium"})])
def test_parent_completion_rejects_mismatched_model_effort_without_timing(tmp_path, model, effort):
    task, action_file = _task(tmp_path)
    reviewed = codex_review_worker.review(task, executor=lambda *_: codex_review_worker.CodexExecution(
        None, {"status": "unknown"}, decision={"kind": "preaudit_review_decision"},
        event_counts={"turn.completed": 1}, event_status="recorded",
    ))
    reviewed.update(model=model, reasoning_effort=effort)
    result = codex_review_worker.complete_review_result(action_file, reviewed,
        completer=lambda *_: pytest.fail("mismatched identity must not enter completion"), request_stop=lambda: None)
    assert result["error_code"] == "codex_review_result_invalid" and result["write_state"] == "not_requested"
    assert not action_file.with_name("agent-timing.json").exists()


def test_parent_completion_preserves_legacy_model_identity_in_new_timing(tmp_path):
    task, action_file = _task(tmp_path)
    reviewed = codex_review_worker.review(task, executor=lambda *_: codex_review_worker.CodexExecution(
        None, {"status": "unknown"}, decision={"kind": "preaudit_review_decision"},
        event_counts={"turn.completed": 1}, event_status="recorded",
    ))
    reviewed.update(model="deepseek-v4-flash", reasoning_effort="high")
    reviewed.pop("model_provider")
    reviewed.pop("requested_service_tier")
    result = codex_review_worker.complete_review_result(action_file, reviewed,
        completer=lambda *_: "codex_decision_invalid", request_stop=lambda: None)
    timing = json.loads(action_file.with_name("agent-timing.json").read_text())
    assert result["model"] == timing["model"] == "deepseek-v4-flash"
    assert result["reasoning_effort"] == timing["reasoning_effort"] == "high"
    assert "requested_service_tier" not in timing


@pytest.mark.parametrize("change", [
    {"model_provider": "openai"}, {"requested_service_tier": "priority"},
    {"model_provider": None}, {"requested_service_tier": None},
    {"model": "deepseek-v4-flash", "reasoning_effort": "high"},
])
def test_parent_completion_rejects_provider_tier_or_mixed_historical_identity(tmp_path, change):
    task, action_file = _task(tmp_path)
    reviewed = codex_review_worker.review(task, executor=lambda *_: codex_review_worker.CodexExecution(
        None, {"status": "unknown"}, decision={"kind": "preaudit_review_decision"},
        event_counts={"turn.completed": 1}, event_status="recorded",
    ))
    reviewed.update(change)
    stopped = []
    result = codex_review_worker.complete_review_result(action_file, reviewed,
        completer=lambda *_: pytest.fail("invalid identity must not enter completion"),
        request_stop=lambda: stopped.append(True))
    assert result["error_code"] == "codex_review_result_invalid"
    assert result["write_state"] == "not_requested" and stopped == [True]
    assert not action_file.with_name("agent-timing.json").exists()


@pytest.fixture(autouse=True)
def fake_review_api_key(monkeypatch, retained_deepseek_cli):
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-fake-key")


@pytest.mark.parametrize("key", ["", "   "])
def test_missing_deepseek_key_never_starts_model_or_uses_native_auth(tmp_path, monkeypatch, key):
    task, action_file = _task(tmp_path)
    monkeypatch.setenv("OA_REVIEW_API_KEY", key)
    monkeypatch.setenv("OPENAI_API_KEY", "unusable-native-key")
    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", lambda *a, **kw: pytest.fail("model started without DeepSeek key"))
    result = codex_review_worker._execute_codex(Path(task["project_root"]), action_file, 60)
    assert result.error_code == "codex_process_start_failed"
    assert result.process_state == "start_failed"
