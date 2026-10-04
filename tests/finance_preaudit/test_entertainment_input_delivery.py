"""Worker → FlowEngine preview; the only replacement is the external reviewer CLI."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import fitz
import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import codex_review_worker as worker, flow_engine


def forbidden(*_args, **_kwargs):
    raise AssertionError("OA, authorization, submission and recovery are forbidden")


def prepare(tmp_path, *, page_count=5, width=16, height=16, degraded=False, tail="完整尾值"):
    root = tmp_path / "project"

    def collect(request_id, attempt):
        if degraded:
            raise ValueError("material_relation_untrusted")
        resources, images, manifest = [], [], []
        for index in range(page_count):
            resource_id = f"resource-{index}"
            source = attempt / "attachments" / f"source-{index}.png"
            page = attempt / "prepared" / f"page-{index}.png"
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), False)
            pix.clear_with(255)
            raw = pix.tobytes("png")
            source.write_bytes(raw)
            page.write_bytes(raw)
            resources.append({"request_id": request_id, "resource_id": resource_id,
                              "source_resource_id": resource_id, "source_system": "oa",
                              "owner_detail_id": f"row-{index}", "local_readonly_path": str(source)})
            images.append({"resource_id": resource_id, "path": str(page), "kind": "image",
                           "page_number": 1, "content_sha256": hashlib.sha256(raw).hexdigest()})
            manifest.append({"resource_id": resource_id, "status": "image", "image_count": 1})
        return {"schema_version": 1, "kind": "preaudit_material", "request_id": request_id,
                "workflow_key": "business_entertainment", "workflow_name": "业务招待费报销流程",
                "declared_fields": [{"path": "detail[0].row[0].field[6]", "value": None,
                                     "read_status": "unresolved", "value_source": "none", "name": tail}],
                "resources": resources, "prepared": {"images": images, "structured_contents": [], "manifest": manifest},
                "authority_facts": [], "warnings": []}

    engine = flow_engine.FlowEngine(root=root / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda workflow: [{"request_id": "90001", "workflow_key": workflow,
                                               "received_at": "2026-10-04T08:00:00+08:00"}],
        collect_material=collect, submit_frozen=forbidden)
    action = engine.next_action("business_entertainment")
    assert action["action"] == "review_required", json.dumps(action)
    return engine, {"project_root": str(root), "action_file": action["action_file"], "timeout_seconds": 60}


def reviewer(monkeypatch, tmp_path, task, *, direct=True, image_count=5, unknown_usage=False, wrong_token=False, resource_evidence=False):
    """A real controllable subprocess, replacing the external paid CLI only."""
    action = json.loads(Path(task["action_file"]).read_text())
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    decision = {"schema_version": 1, "kind": "preaudit_review_decision", "action_token": action["action_token"],
                "workflow_key": "business_entertainment", "rule_version": "business_entertainment:1",
                "overall": "needs_human", "items": [{"item_key": item["item_key"], "status": "uncertain",
                    "conclusion": "自动读取未完成核对，请财务查看原单。", "evidence_refs": [],
                    "missing_evidence": ["本项支付与费用的对应关系尚未确认。"]} for item in packet["items"]]}
    if resource_evidence:
        decision["items"][0]["evidence_refs"] = ["material:prepared.images.resource-0.page-1"]
    if wrong_token:
        decision["action_token"] = "wrong-token"
    script = tmp_path / "reviewer.py"
    script.write_text('''import json,sys
from pathlib import Path
argv=sys.argv[1:];prompt=sys.stdin.read()
direct=''' + repr(direct) + '''
if direct:
 if ''' + repr(resource_evidence) + ''':
  assert "evidence_refs 的表单字段引用必须逐字使用" in prompt
 assert "冻结数据：material" in prompt
 assert "当前阶段按既定策略暂不校验" in prompt
 assert "冻结输入原文结束。" in prompt
else:
 assert "冻结数据：material" not in prompt
images=[argv[i+1] for i,x in enumerate(argv) if x=="--image"]
assert len(images)==''' + str(image_count) + '''
assert all(Path(p).read_bytes().startswith(b"\\x89PNG") for p in images)
schema=json.loads(Path(argv[argv.index("--output-schema")+1]).read_text())
assert set(schema["required"])=={"schema_version","kind","action_token","workflow_key","rule_version","overall","items"}
Path(argv[argv.index("--output-last-message")+1]).write_text(''' + repr(json.dumps(decision, ensure_ascii=False)) + ''')
print(json.dumps({"type":"thread.started"}))
print(json.dumps({"type":"turn.started"}))
''' + ('''print(json.dumps({"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":50,"output_tokens":10}}))
''' if not unknown_usage else '''print(json.dumps({"type":"turn.completed"}))
'''), encoding="utf-8")
    original = subprocess.Popen
    calls = []

    def start(command, **kwargs):
        calls.append(command)
        return original([sys.executable, str(script), *command[1:]], **kwargs)

    monkeypatch.setattr(worker.subprocess, "Popen", start)
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    return calls


def close(engine, task, reviewed):
    assert reviewed["status"] == "reviewed", reviewed
    decision = Path(task["action_file"]).with_name("candidate-decision.json")
    json_artifact.write_json_atomic(decision, reviewed["decision"])
    return engine.complete_review(Path(task["action_file"]), decision)


def test_all_five_pages_and_complete_rules_reach_preview(monkeypatch, tmp_path):
    engine, task = prepare(tmp_path)
    calls = reviewer(monkeypatch, tmp_path, task)
    reviewed = worker.review(task)
    closed = close(engine, task, reviewed)
    assert closed["action"] == "closed"
    assert closed["business_state"] == "needs_human"
    assert closed["write_state"] == "not_requested"
    remark = Path(closed["remark_file"]).read_text()
    assert all(name in remark for name in ("附件完整性/支付证据", "发票关联", "预算", "金额孰低核销"))
    assert len(calls) == 1
    assert reviewed["token_usage"]["total_tokens"] == 110


def test_degraded_inputs_reach_finance_gap_result(monkeypatch, tmp_path):
    engine, task = prepare(tmp_path, degraded=True)
    calls = reviewer(monkeypatch, tmp_path, task, image_count=0)
    closed = close(engine, task, worker.review(task))
    assert closed["action"] == "closed"
    assert closed["business_state"] == "needs_human"
    assert closed["write_state"] == "not_requested"
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["material", "token", "page", "missing", "symlink", "outside"])
def test_untrusted_inputs_never_start_reviewer(monkeypatch, tmp_path, change):
    _engine, task = prepare(tmp_path)
    action_file = Path(task["action_file"])
    material_file = action_file.with_name("material.json")
    material = json.loads(material_file.read_text())
    page = Path(material["prepared"]["images"][0]["path"])
    if change == "material":
        material_file.write_text(material_file.read_text() + " ")
    elif change == "token":
        state_file = action_file.with_name("state.json")
        state = json.loads(state_file.read_text())
        state["action_token"] = "wrong-token"
        json_artifact.write_json_atomic(state_file, state)
    elif change == "page":
        page.write_bytes(b"corrupted-page")
    elif change == "missing":
        page.unlink()
    elif change == "symlink":
        original = tmp_path / "outside.png"
        original.write_bytes(page.read_bytes())
        page.unlink()
        page.symlink_to(original)
    else:
        material["prepared"]["images"][0]["path"] = str(tmp_path / "outside.png")
        json_artifact.write_json_atomic(material_file, material)
    monkeypatch.setattr(worker.subprocess, "Popen", forbidden)
    monkeypatch.delenv("OA_REVIEW_API_KEY", raising=False)
    reviewed = worker.review(task)
    assert reviewed["status"] == "failed"
    assert reviewed["error_code"] == "codex_review_task_invalid"
    assert reviewed["write_state"] == "not_requested"
    assert reviewed["token_usage"] == {"status": "unknown"}


def test_text_limit_retains_existing_path_without_truncation(monkeypatch, tmp_path):
    engine, task = prepare(tmp_path, tail="原值" * 60_000)
    calls = reviewer(monkeypatch, tmp_path, task, direct=False, image_count=0)
    closed = close(engine, task, worker.review(task))
    assert closed["action"] == "closed"
    assert closed["write_state"] == "not_requested"
    assert len(calls) == 1


def test_different_image_preparation_uses_existing_path(monkeypatch, tmp_path):
    engine, task = prepare(tmp_path, width=2048, height=2048)
    calls = reviewer(monkeypatch, tmp_path, task, direct=False, image_count=0)
    closed = close(engine, task, worker.review(task))
    assert closed["action"] == "closed"
    assert closed["write_state"] == "not_requested"
    assert len(calls) == 1


def test_missing_usage_stays_unknown_after_preview_closes(monkeypatch, tmp_path):
    engine, task = prepare(tmp_path)
    reviewer(monkeypatch, tmp_path, task, unknown_usage=True)
    reviewed = worker.review(task)
    closed = close(engine, task, reviewed)
    assert closed["action"] == "closed"
    assert reviewed["token_usage"] == {"status": "unknown"}


def test_conflicting_decision_is_rejected_by_existing_consumer(monkeypatch, tmp_path):
    engine, task = prepare(tmp_path)
    reviewer(monkeypatch, tmp_path, task, wrong_token=True)
    reviewed = worker.review(task)
    assert reviewed["status"] == "reviewed"
    with pytest.raises(ValueError, match="review_decision_invalid"):
        close(engine, task, reviewed)
    action = json.loads(Path(task["action_file"]).read_text())
    assert action["action"] == "review_required"
    assert action["write_state"] == "not_requested"


def test_delivery_receipt_reports_fallback_separately(monkeypatch, tmp_path):
    engine, task = prepare(tmp_path, width=2048, height=2048)
    reviewer(monkeypatch, tmp_path, task, direct=False, image_count=0)
    closed = close(engine, task, worker.review(task))
    assert closed["action"] == "closed"
    files = list(Path(task["action_file"]).parent.glob("review-process-*.json"))
    assert len(files) == 1
    diagnostic = json.loads(files[0].read_text())
    assert diagnostic["input_delivery"] == {"mode": "existing_reading_path",
        "fallback_reason": "image_preparation_differs", "images_supplied": 0}


def test_original_page_references_remain_consumable(monkeypatch, tmp_path):
    engine, task = prepare(tmp_path)
    reviewer(monkeypatch, tmp_path, task, resource_evidence=True)
    reviewed = worker.review(task)
    closed = close(engine, task, reviewed)
    assert closed["action"] == "closed"
    assert reviewed["decision"]["items"][0]["evidence_refs"] == ["material:prepared.images.resource-0.page-1"]


def test_symlinked_action_entry_never_starts_reviewer(monkeypatch, tmp_path):
    _engine, task = prepare(tmp_path)
    alias = tmp_path / "action.json"
    alias.symlink_to(task["action_file"])
    task["action_file"] = str(alias)
    monkeypatch.setattr(worker.subprocess, "Popen", forbidden)
    monkeypatch.delenv("OA_REVIEW_API_KEY", raising=False)
    reviewed = worker.review(task)
    assert reviewed["error_code"] == "codex_review_task_invalid"
    assert reviewed["status"] == "failed"


def test_large_original_sources_are_delivered_in_existing_cli_path(monkeypatch, tmp_path):
    engine, task = prepare(tmp_path, page_count=1, width=1200, height=2800)
    material = json.loads(Path(task["action_file"]).with_name("material.json").read_text())
    originals = {p["path"]: hashlib.sha256(Path(p["path"]).read_bytes()).hexdigest()
                 for p in material["prepared"]["images"]}
    reviewer(monkeypatch, tmp_path, task, image_count=1)
    reviewed = worker.review(task)
    closed = close(engine, task, reviewed)
    assert closed["action"] == "closed"
    assert closed["write_state"] == "not_requested"
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == sha for p, sha in originals.items())


def test_small_compressed_sources_do_not_hide_reencoding_capacity(monkeypatch, tmp_path):
    engine, task = prepare(tmp_path, width=1200, height=2800)
    reviewer(monkeypatch, tmp_path, task, direct=False, image_count=0)
    closed = close(engine, task, worker.review(task))
    assert closed["action"] == "closed"
    diagnostic = json.loads(next(Path(task["action_file"]).parent.glob("review-process-*.json")).read_text())
    assert diagnostic["input_delivery"]["fallback_reason"] == "request_bytes_limit"
