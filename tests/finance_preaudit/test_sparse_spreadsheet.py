import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

import openpyxl
import pytest


def _sparse_workbook(path, *, distant_cell=False):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "明细"
    sheet["A1"] = "first"
    sheet.merge_cells("A1:C1")
    sheet["B3"] = "=1+1"
    sheet["XFD3"] = "far column"
    if distant_cell:
        sheet["XFD1048576"] = "last"
    workbook.save(path)
    workbook.close()
    with zipfile.ZipFile(path) as archive:
        parts = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    xml = parts["xl/worksheets/sheet1.xml"]
    xml = re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1:XFD1048576"', xml)
    parts["xl/worksheets/sheet1.xml"] = re.sub(
        rb"(<f>1\+1</f>)<v(?:\s*/>|></v>)", rb"\g<1><v>2</v>", xml,
    )
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


@pytest.mark.parametrize("distant_cell", [False, True])
def test_sparse_reader_ignores_declared_dimension_without_losing_distant_values(tmp_path, distant_cell):
    source = tmp_path / "sparse.xlsx"
    _sparse_workbook(source, distant_cell=distant_cell)
    command = (
        "import json,sys; from pathlib import Path; "
        "from apps.oa_approval.attachment_extractors import spreadsheet_fact; "
        "print(json.dumps(spreadsheet_fact(Path(sys.argv[1]),resource_id='source',name='sparse.xlsx'),ensure_ascii=False))"
    )
    result = subprocess.run(
        [sys.executable, "-c", command, str(source)], capture_output=True,
        text=True, timeout=5, cwd=Path(__file__).resolve().parents[2],
    )
    assert result.returncode == 0, result.stderr
    fact = json.loads(result.stdout)
    assert fact["key_text"][:2] == [
        "明细!R1: C1=first", "明细!R3: C2=2 | C16384=far column"
    ]
    assert len(fact["key_text"]) == (3 if distant_cell else 2)
    if distant_cell:
        assert fact["key_text"][-1] == "明细!R1048576: C16384=last"
        assert "1048576行16384列" in fact["summary"]
    else:
        assert "3行16384列" in fact["summary"]
