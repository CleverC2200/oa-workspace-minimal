from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import read_material, flow_engine


def test_every_material_value_and_order_survives_readable_view():
    material = {
        "declared_fields": [
            {"path": "detail[0].row[0].field[6]", "name": "同名", "value": "quoted\nvalue", "read_status": "unresolved"},
            {"path": "detail[0].row[6].field[6]", "name": "同名", "value": None},
            {"path": "detail[0].row[6].field[7]", "name": "同名",
             "value": {"nested": [False, 0, None]}, "extra": "保留属性"},
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


def files(tmp_path: Path) -> Path:
    material = {
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


@pytest.mark.parametrize('fields', [None, '原字符串', {'path': 'detail[0].row[0]'}])
def test_readable_view_preserves_non_field_array_values_without_invented_locations(fields):
    text = read_material.render({'declared_fields': fields})
    assert json.dumps(fields, ensure_ascii=False, separators=(',', ':')) in text
    assert '\n明细定位显示：' not in text
