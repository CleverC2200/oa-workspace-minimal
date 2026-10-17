from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from apps.oa_finance_preaudit import tpm_price_reference as prices
from apps.oa_finance_preaudit import codex_review_worker, flow_engine, read_material
from tests.finance_preaudit.test_tpm_small_marketing import _engine, _decision, WORKFLOW_KEY


def _catalog(tmp_path: Path) -> dict:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "参考报价单"
    sheet.append(["序号", "类型", "材料名称", "材料编码", "材质说明/要求", "单位", "未税指导价（元）", "含税指导价（元，1%）"])
    sheet.append([1, "合成测试", "测试板", "001", "5mm", "㎡", 99, 20])
    path = tmp_path / "source.xlsx"
    workbook.save(path)
    return prices.read_workbook(path)


def _material(subject: str = "终端建设费-终端形象制作费") -> dict:
    return {"declared_fields": [{"code": "yskm", "path": "detail[0].row[0].field[2]", "read_status": "present", "value": subject}]}


def test_import_preserves_price_column_units_and_source(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    assert catalog["rows"] == [{"source_row": 2, "material_code": "001", "name": "测试板", "specification": "5mm", "unit": "㎡", "guide_price": "20"}]
    assert catalog["price_column"] == "H"
    assert len(catalog["source_sha256"]) == 64
    assert catalog["sheet"] == "参考报价单"


@pytest.mark.parametrize("fault", ["negative", "nan", "duplicate", "missing_spec", "wrong_column"])
def test_invalid_catalog_is_a_price_gap_only(tmp_path: Path, fault: str) -> None:
    catalog = _catalog(tmp_path)
    if fault == "negative":
        catalog["rows"][0]["guide_price"] = "-1"
    elif fault == "nan":
        catalog["rows"][0]["guide_price"] = "NaN"
    elif fault == "duplicate":
        catalog["rows"].append(dict(catalog["rows"][0]))
    elif fault == "missing_spec":
        del catalog["rows"][0]["specification"]
    else:
        catalog["price_column"] = "G"
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(catalog))
    assert prices.snapshot(path)["status"] == "unreadable"
    assert prices.snapshot(tmp_path / "absent.json") == {"status": "not_configured"}


def test_scope_is_exact_and_row_driven() -> None:
    assert prices.applies(_material())
    assert not prices.applies(_material("推广费用-物料费"))
    assert not prices.applies({"declared_fields": [], "title": "终端建设费-终端形象制作费"})
    mixed = _material("推广费用-物料费")
    mixed["declared_fields"] += _material()["declared_fields"]
    assert prices.applies(mixed)


def test_pass_requires_a_real_frozen_catalog_row(tmp_path: Path) -> None:
    material = {**_material(), "tpm_price_reference": {"status": "ready", **_catalog(tmp_path)}}
    item = {"item_key": "attachment_completeness", "status": "pass", "conclusion": "附件完整。", "evidence_refs": ["material:tpm_price_reference.rows.999"], "missing_evidence": []}
    assert prices.evidence_gap(item, material)
    item["evidence_refs"] = ["material:tpm_price_reference.rows.0"]
    assert prices.evidence_gap(item, material) is None
    item["item_key"] = "reimbursable_minimum"
    assert prices.evidence_gap(item, material) is None


def test_quote_data_is_visible_in_lossless_material_reader(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    rendered = read_material.render({"tpm_price_reference": catalog})
    assert "material.tpm_price_reference" in rendered
    assert '"guide_price":"20"' in rendered
    assert catalog["source_sha256"] in rendered


@pytest.mark.parametrize("status", ["fail", "uncertain"])
def test_reference_guard_keeps_existing_business_problems(status: str) -> None:
    item = {"item_key": "attachment_completeness", "status": status}
    assert prices.evidence_gap(item, _material()) is None


def test_ready_catalog_allows_first_item_and_keeps_amount(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "reference" / "tpm-price-reference.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_catalog(tmp_path)))
    fields = _material()["declared_fields"] + [{
        "code": "glfp", "name": "关联发票", "path": "detail[0].row[0].field[17]",
        "read_status": "present", "value": "12345678901234567890",
    }]
    engine = _engine(tmp_path, declared_fields=fields)
    action = engine.next_action(WORKFLOW_KEY)
    decision = _decision(action, conclusion="附件完整。")
    decision["overall"] = "pass"
    for item in decision["items"]:
        item.update(status="pass", missing_evidence=[], evidence_refs=["detail[0].row[0].field[2]"])
    decision["items"][0]["evidence_refs"].append("material:tpm_price_reference.rows.0")
    decision["items"][2]["conclusion"] = "按 500 元核销。"
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(json.dumps(decision, ensure_ascii=False))
    result = engine.complete_review(Path(action["action_file"]), decision_file)
    assert result["business_state"] == "pass"
    assert "按 500 元核销。" in Path(result["remark_file"]).read_text()


def test_v3_frozen_packet_can_be_reconstructed_but_not_resumed() -> None:
    expected = {"rule_version": "tpm_small_marketing_fee:4", "items": [{"item_key": "attachment_completeness"}]}
    state = {"rule_version": "tpm_small_marketing_fee:3"}
    historical = flow_engine._accepted_tpm_small_marketing_packets(expected, state)[0]
    assert historical == {**expected, **state}
    with pytest.raises(ValueError, match="frozen_packet_changed"):
        flow_engine._accepted_tpm_small_marketing_packets(expected, state, action=state)


def test_worker_routes_price_review_into_first_item(tmp_path: Path) -> None:
    action = _engine(tmp_path).next_action(WORKFLOW_KEY)
    prompt = codex_review_worker._prompt(Path(action["action_file"]))
    assert "价格问题只归第 1 项" in prompt
    assert "不按发票税率换算" in prompt
    assert "不以指导价乘数量压低核销金额" in prompt
    assert "区间参考" not in prompt


def test_engine_freezes_reference_and_ignores_later_updates(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "reference" / "tpm-price-reference.json"
    path.parent.mkdir(parents=True)
    catalog = _catalog(tmp_path)
    path.write_text(json.dumps(catalog))
    engine = _engine(tmp_path, declared_fields=_material()["declared_fields"])
    action = engine.next_action(WORKFLOW_KEY)
    material_file = Path(action["action_file"]).with_name("material.json")
    frozen = json.loads(material_file.read_text())
    assert frozen["tpm_price_reference"]["rows"] == catalog["rows"]
    path.write_text("invalid after freeze")
    engine._open_attempt(Path(action["action_file"]))
    assert json.loads(material_file.read_text()) == frozen


def test_missing_reference_only_downgrades_first_item_and_keeps_amount(tmp_path: Path) -> None:
    engine = _engine(tmp_path, declared_fields=_material()["declared_fields"])
    action = engine.next_action(WORKFLOW_KEY)
    decision = _decision(action, conclusion="附件完整。")
    decision["overall"] = "pass"
    for item in decision["items"]:
        item.update(status="pass", missing_evidence=[], evidence_refs=["detail[0].row[0].field[2]"])
    decision["items"][2]["conclusion"] = "按 500 元核销。"
    original = copy.deepcopy(decision)
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(json.dumps(decision, ensure_ascii=False))
    candidate_bytes = decision_file.read_bytes()
    result = engine.complete_review(Path(action["action_file"]), decision_file)
    remark = Path(result["remark_file"]).read_text()
    assert result["write_state"] == "not_requested"
    assert result["business_state"] == "needs_human"
    assert "⚠️ 1、附件完整性：" in remark
    assert "参考报价" in remark
    assert "✅ 3、金额孰低核销：按 500 元核销。" in remark
    assert len(remark.splitlines()) == 5
    saved_file = Path(result["decision_file"])
    saved = json.loads(saved_file.read_text())
    receipt = json.loads(Path(result["receipt_file"]).read_text())
    item = saved["items"][0]
    assert item["status"] == "uncertain"
    assert item["conclusion"] in item["missing_evidence"]
    assert "⚠️ 1、附件完整性：" + item["conclusion"] in remark
    assert saved["items"][2:] == original["items"][2:]
    assert saved["overall"] == receipt["business_state"] == result["business_state"]
    assert receipt["decision_fingerprint"] == "sha256:" + hashlib.sha256(saved_file.read_bytes()).hexdigest()
    assert decision_file.read_bytes() == candidate_bytes
