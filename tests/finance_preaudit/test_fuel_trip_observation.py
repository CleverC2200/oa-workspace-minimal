from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import FlowEngine
from apps.oa_finance_preaudit import codex_review_worker


def _review(tmp_path: Path, *, car_mode: str = "按次", duplicate: bool = False):
    def collect(request_id: str, root: Path) -> dict:
        source = root / "attachments" / "mileage.bin"
        source.write_bytes(b"synthetic-mileage-source")
        images = []
        for index in range(5 if duplicate else 4):
            content = f"synthetic-page-{index if index < 4 else 0}".encode()
            image = root / "prepared" / f"{index}.png"
            image.write_bytes(content)
            images.append({"resource_id": "mileage", "path": str(image),
                           "content_sha256": hashlib.sha256(content).hexdigest()})
        return {
            "schema_version": 1, "kind": "preaudit_material",
            "request_id": request_id, "workflow_key": "travel_expense",
            "workflow_name": "差旅费报销流程",
            "declared_fields": [{"path": "main[0].field[0]", "code": "sfdc",
                                 "name": "是否带车", "value": car_mode, "read_status": "present"}],
            "resources": [{"request_id": request_id, "resource_id": "mileage",
                           "source_resource_id": "mileage", "source_system": "oa",
                           "local_readonly_path": str(source)}],
            "prepared": {"images": images, "structured_contents": [],
                         "manifest": [{"resource_id": "mileage", "status": "image"}]},
            "authority_facts": [], "warnings": [],
        }
    engine = FlowEngine(root=tmp_path, discover_candidates=lambda key: [
        {"request_id": "synthetic-trip", "received_at": "2026-09-01T09:00:00+08:00", "workflow_key": key}
    ], collect_material=collect)
    action = engine.next_action("travel_expense", mode="preview")
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    refs = packet["page_routing"]["review_page_refs"]
    decision = {
        "schema_version": 1, "kind": "preaudit_review_decision",
        "action_token": action["action_token"], "workflow_key": "travel_expense",
        "rule_version": packet["rule_version"], "overall": "pass",
        "items": [{"item_key": item["item_key"], "status": "pass", "conclusion": "已核对本项。",
                   "evidence_refs": ["main[0].field[0]", *refs], "missing_evidence": []}
                  for item in packet["items"]],
    }
    return engine, action, packet, decision


def test_per_trip_review_rejects_unobserved_whole_period_shortcut(tmp_path: Path) -> None:
    engine, action, _packet, decision = _review(tmp_path)
    decision["items"][5]["conclusion"] = "起始83859，结束85716，差值1857，与申报一致。"
    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.validate_review_decision(Path(action["action_file"]), decision)


def _groups() -> dict:
    return {"outcome": "grouped", "groups": [
        {"trip_basis": "首次带车行程，9月1日至9月5日", "basis_refs": ["main[0].field[0]"],
         "start": {"reading_km": 83859, "locator": "9月1日累计里程", "image_ref": "material:prepared.images.mileage.1"},
         "end": {"reading_km": 85100, "locator": "9月5日累计里程", "image_ref": "material:prepared.images.mileage.2"},
         "distance_km": 1241},
        {"trip_basis": "第二次带车行程，9月11日至9月14日", "basis_refs": ["main[0].field[0]"],
         "start": {"reading_km": 85105, "locator": "9月11日累计里程", "image_ref": "material:prepared.images.mileage.3"},
         "end": {"reading_km": 85716, "locator": "9月14日累计里程", "image_ref": "material:prepared.images.mileage.4"},
         "distance_km": 611},
    ], "total_km": 1852}


def test_per_trip_observation_closes_preview_with_correct_group_sum(tmp_path: Path) -> None:
    engine, action, _packet, decision = _review(tmp_path)
    decision["fuel_trip_observation"] = _groups()
    decision["overall"] = "needs_human"
    decision["items"][5].update(status="fail", conclusion="两次里程1241+611=1852公里，申报1857公里，多计5公里。")
    path = tmp_path / "decision.json"
    json_artifact.write_json_atomic(path, decision)
    closed = engine.complete_review(Path(action["action_file"]), path)
    assert (closed["business_state"], closed["write_state"]) == ("needs_human", "not_requested")
    assert engine.complete_review(Path(action["action_file"]), path)["action"] == "closed"


def test_actual_worker_schema_and_prompt_require_frozen_group_observation(tmp_path: Path) -> None:
    _engine, action, _packet, _decision = _review(tmp_path)
    schema = codex_review_worker._decision_schema(Path(action["action_file"]))
    assert "fuel_trip_observation" in schema["required"]
    assert "fuel_trip_observation" in codex_review_worker._prompt(Path(action["action_file"]))


def test_actual_worker_prompts_do_not_disclose_recorded_trip_answers(tmp_path: Path) -> None:
    _engine, action, _packet, _decision = _review(tmp_path)
    action_file = Path(action["action_file"])
    for prompt in (codex_review_worker._prompt(action_file), codex_review_worker._read_only_prompt(action_file)):
        assert "逐组" in prompt
        assert "不得以全体最大值减最小值" in prompt
        for recorded_value in ("83859", "85100", "85105", "85716", "1852", "1857"):
            assert recorded_value not in prompt
        assert "不得读取" in prompt and "regression-cases.json" in prompt
        assert "测试" in prompt and "私有验收预期" in prompt


def test_worker_normalization_preserves_model_owned_observation(tmp_path: Path) -> None:
    engine, action, _packet, decision = _review(tmp_path)
    decision["fuel_trip_observation"] = _groups()
    result = codex_review_worker._normalize_decision(Path(action["action_file"]), decision)
    assert result["fuel_trip_observation"] == _groups()
    assert result["items"] == decision["items"]
    engine.validate_review_decision(Path(action["action_file"]), result)


@pytest.mark.parametrize("invalid", ["whole_period_total", "invoice_row_split", "unowned_image", "missing_locator", "duplicate_trip"])
def test_per_trip_contract_rejects_invalid_model_observations(tmp_path: Path, invalid: str) -> None:
    engine, action, _packet, decision = _review(tmp_path, duplicate=True)
    observation = _groups()
    if invalid == "whole_period_total":
        observation["total_km"] = 1857
    elif invalid == "invoice_row_split":
        observation["groups"][0]["distance_km"] = 1740
        observation["groups"][1]["distance_km"] = 117
        observation["total_km"] = 1857
    elif invalid == "unowned_image":
        observation["groups"][0]["start"]["image_ref"] = "material:prepared.images.other.1"
    elif invalid == "missing_locator":
        observation["groups"][0]["start"]["locator"] = ""
    elif invalid == "duplicate_trip":
        repeated = json.loads(json.dumps(observation["groups"][0]))
        repeated["start"]["image_ref"] = "material:prepared.images.mileage.5"
        observation["groups"].append(repeated)
        observation["total_km"] = 3093
        decision["items"][5]["evidence_refs"].append("material:prepared.images.mileage.5")
    decision["fuel_trip_observation"] = observation
    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.validate_review_decision(Path(action["action_file"]), decision)


def test_unknown_pairing_closes_as_business_uncertainty(tmp_path: Path) -> None:
    engine, action, _packet, decision = _review(tmp_path)
    decision["fuel_trip_observation"] = {"outcome": "uncertain", "groups": [], "total_km": None}
    decision["overall"] = "needs_human"
    decision["items"][5].update(status="uncertain", conclusion="第二次行程起止无法配对，请核对。", missing_evidence=["第二次行程配对依据"])
    path = tmp_path / "decision.json"
    json_artifact.write_json_atomic(path, decision)
    assert engine.complete_review(Path(action["action_file"]), path)["business_state"] == "needs_human"


def test_one_confirmed_trip_is_valid_without_inventing_another(tmp_path: Path) -> None:
    engine, action, _packet, decision = _review(tmp_path)
    observation = _groups()
    observation["groups"] = observation["groups"][:1]
    observation["total_km"] = 1241
    decision["fuel_trip_observation"] = observation
    engine.validate_review_decision(Path(action["action_file"]), decision)


def test_no_fuel_claim_accepts_explicit_non_applicability(tmp_path: Path) -> None:
    engine, action, _packet, decision = _review(tmp_path)
    decision["fuel_trip_observation"] = {"outcome": "not_applicable", "groups": [], "total_km": None}
    decision["items"][5]["conclusion"] = "未申报油补。"
    engine.validate_review_decision(Path(action["action_file"]), decision)


def test_single_interval_keeps_original_decision_contract(tmp_path: Path) -> None:
    engine, action, packet, decision = _review(tmp_path, car_mode="是")
    assert "fuel_trip_contract" not in packet
    engine.validate_review_decision(Path(action["action_file"]), decision)
    assert "fuel_trip_observation" not in codex_review_worker._decision_schema(Path(action["action_file"]))["properties"]


def test_old_marker_free_packet_keeps_its_original_contract(tmp_path: Path) -> None:
    engine, action, packet, decision = _review(tmp_path)
    # Simulate an already-frozen pre-upgrade fixture; production evidence is never edited.
    material_path = Path(packet["material_file"])
    old_material = json.loads(material_path.read_text())
    old_material.pop("fuel_trip_contract")
    json_artifact.write_json_atomic(material_path, old_material, indent=2)
    old_fingerprint = "sha256:" + hashlib.sha256(material_path.read_bytes()).hexdigest()
    for path in (Path(action["action_file"]), Path(action["state_file"]), Path(action["review_packet_file"])):
        old = json.loads(path.read_text())
        old.pop("fuel_trip_contract")
        if "material_fingerprint" in old:
            old["material_fingerprint"] = old_fingerprint
        json_artifact.write_json_atomic(path, old)
    engine.validate_review_decision(Path(action["action_file"]), decision)
    assert "fuel_trip_observation" not in codex_review_worker._decision_schema(Path(action["action_file"]))["properties"]


@pytest.mark.parametrize("artifact", ["action_file", "state_file", "review_packet_file"])
def test_new_marker_cannot_be_removed_from_one_frozen_artifact(tmp_path: Path, artifact: str) -> None:
    engine, action, _packet, decision = _review(tmp_path)
    path = Path(action[artifact])
    changed = json.loads(path.read_text())
    changed.pop("fuel_trip_contract")
    json_artifact.write_json_atomic(path, changed)
    with pytest.raises(ValueError, match="frozen_packet_changed"):
        engine.validate_review_decision(Path(action["action_file"]), decision)


def test_new_contract_cannot_be_downgraded_by_removing_all_protocol_markers(tmp_path: Path) -> None:
    engine, action, _packet, decision = _review(tmp_path)
    for name in ("action_file", "state_file", "review_packet_file"):
        path = Path(action[name])
        changed = json.loads(path.read_text())
        changed.pop("fuel_trip_contract")
        json_artifact.write_json_atomic(path, changed)
    with pytest.raises(ValueError, match="frozen_packet_changed"):
        engine.validate_review_decision(Path(action["action_file"]), decision)
    with pytest.raises(ValueError, match="frozen_packet_changed"):
        codex_review_worker._decision_schema(Path(action["action_file"]))
