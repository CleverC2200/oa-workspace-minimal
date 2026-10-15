from pathlib import Path

import pytest

from apps.oa_approval import attachment_extractors


# Synthetic BIFF8 workbook, exported with LibreOffice from two generated sheets.
FIXTURE = Path(__file__).parents[1] / "fixtures" / "legacy-workbook.xls"


def test_legacy_xls_preparation_reads_real_workbook(tmp_path):
    assert FIXTURE.read_bytes().startswith(bytes.fromhex("d0cf11e0a1b11ae1"))
    images, manifest, facts = attachment_extractors.prepare_materials(
        [{"resource_id": "legacy-book", "name": FIXTURE.name, "path": str(FIXTURE)}],
        out_dir=tmp_path / "prepared",
    )

    assert images == []
    assert manifest == [{
        "resource_id": "legacy-book", "name": FIXTURE.name,
        "status": "legacy_spreadsheet_read", "path": str(FIXTURE),
    }]
    assert len(facts) == 1
    assert facts[0]["resource_id"] == "legacy-book"
    assert facts[0]["document_type"] == "legacy_spreadsheet"
    assert facts[0]["key_text"] == [
        "明细!R1: 物料 | 数量 | 金额",
        "明细!R2: 合成样品 | 2.0 | 12.5",
        "明细!R3: 第二项 | 3.0 | 7.25",
        "补充!R1: synthetic only",
    ]


@pytest.mark.parametrize("content", [b"invalid workbook", FIXTURE.read_bytes()[:32]])
def test_corrupt_legacy_xls_still_blocks_preparation(tmp_path, content):
    broken = tmp_path / "broken.xls"
    broken.write_bytes(content)

    with pytest.raises(RuntimeError, match="^attachment_content_incomplete$"):
        attachment_extractors.prepare_materials(
            [{"resource_id": "broken-book", "name": broken.name, "path": str(broken)}],
            out_dir=tmp_path / "prepared",
        )
