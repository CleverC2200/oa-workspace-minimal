from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import read_material, flow_engine


def test_every_material_value_and_order_survives_readable_view():
    material = {
        "declared_fields": [
            {"path": "row[0]", "value": "quoted\nvalue", "read_status": "unresolved"},
            {"path": "row[1]", "value": None},
        ],
        "prepared": {"images": [
            {"path": "one.png", "spreadsheet_cell": "照片!B2"},
            {"path": "two.png", "spreadsheet_cell": "照片!C2"},
        ]},
        "warnings": ["unknown relationship"],
    }
    text = read_material.render(material)
    sections = {}
    current = None
    for line in text.splitlines():
        if line.startswith("[") and line.endswith("]") and not line.startswith('["'):
            current = line[1:-1]
            sections[current] = []
        elif current:
            sections[current].append(line)
    field_lines = sections["material.declared_fields"]
    keys = json.loads(field_lines[0].split("=", 1)[1])
    restored = []
    for line in field_lines[1:]:
        if ": " in line and line[0].isdigit():
            restored.append(dict(zip(keys, json.loads(line.split(": ", 1)[1]))))
        elif line.startswith("absent="):
            for key in json.loads(line.split("=", 1)[1]):
                restored[-1].pop(key)
    assert restored == material["declared_fields"]
    assert text.index("one.png") < text.index("two.png")
    assert "照片!B2" in text and "照片!C2" in text
    assert "unknown relationship" in text


def files(tmp_path: Path, material=None) -> Path:
    material = material if material is not None else {
        "workflow_key": "tpm_small_marketing_fee",
        "prepared": {"images": []},
        "declared_fields": [],
    }
    material_file = tmp_path / "material.json"
    packet_file = tmp_path / "review-packet.json"
    material_file.write_text(json.dumps(material))
    packet_file.write_text(json.dumps({
        "workflow_key": material["workflow_key"],
        "material_file": str(material_file),
        "material_fingerprint": flow_engine._file_fingerprint(material_file),
    }))
    action = tmp_path / "action.json"
    action.write_text(json.dumps({
        "workflow_key": material["workflow_key"],
        "review_packet_file": str(packet_file),
    }))
    return action


def test_reader_does_not_modify_frozen_files(tmp_path):
    action = files(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert "[material.prepared.images]" in read_material.read(action)
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_reader_rejects_changed_material(tmp_path):
    action = files(tmp_path)
    (tmp_path / "material.json").write_text("{}")
    with pytest.raises(ValueError, match="frozen_material_changed"):
        read_material.read(action)


def test_reader_rejects_different_action_packet(tmp_path):
    action = files(tmp_path)
    payload = json.loads(action.read_text())
    payload["review_packet_file"] = str(tmp_path / "other-attempt" / "review-packet.json")
    action.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="frozen_material_changed"):
        read_material.read(action)


def test_empty_prepared_mapping_remains_visible():
    assert "[material.prepared]\n{}" in read_material.render({"prepared": {}})


def test_reader_labels_source_rows_per_table_without_using_array_position(tmp_path):
    material = {
        "workflow_key": "business_entertainment",
        "declared_fields": [
            {"path": "main.field[0]", "name": "金额", "value": "120"},
            {"path": "detail[1].row[2].field[4]", "name": "金额", "value": None},
            {"path": "detail[1].row[2].field[7]", "name": "金额", "read_status": "unresolved"},
            {"path": "detail[0].row[0].field[4]", "name": "金额", "value": "120"},
            {"path": "unknown[7]", "value": "unlocated"},
        ],
        "prepared": {"images": [{"path": "page.png", "spreadsheet_cell": "附件!C2"}]},
    }
    action = files(tmp_path, material)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    view = read_material.read(action)
    assert view.count("明细表2第3行：detail[1].row[2]") == 1
    assert "明细表1第1行：detail[0].row[0]" in view
    assert "零基数组索引，不是明细行号" in view
    assert "完整字段路径" in view and "标签不证明业务事实" in view
    assert view.index("main.field[0]") < view.index("detail[1].row[2].field[4]")
    assert view.index("detail[1].row[2].field[7]") < view.index("detail[0].row[0].field[4]")
    assert 'absent=["value"]' in view and "null" in view
    assert "附件!C2" in view and "unlocated" in view
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
