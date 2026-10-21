from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import sys
import zipfile

import pytest

from apps.oa_approval import attachment_extractors as extractors

pytestmark = pytest.mark.usefixtures('recorded_bounded_office_workspace')

def _presentation(tmp_path: Path, pages: int = 4) -> dict:
    source = tmp_path / "presentation.pptx"
    nodes = "".join(
        f'<p:sldId id="{256 + index}" r:id="rId{index + 1}"/>'
        for index in range(pages)
    )
    xml = (
        '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<p:sldIdLst>{nodes}</p:sldIdLst></p:presentation>"
    )
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ppt/presentation.xml", xml)
    return {"resource_id": "pptx-source", "path": str(source), "name": source.name}


def _converter(tmp_path, monkeypatch, pages=4, fail=False):
    binary = tmp_path / "soffice"
    binary.write_text(f"#!{sys.executable}\n" + f'''import pathlib, sys, fitz
args = sys.argv
assert any('impress_pdf_Export' in arg and 'ExportHiddenSlides' in arg and 'true' in arg for arg in args)
assert args[-1].endswith('source.pptx')
if {fail!r}: raise SystemExit(1)
out = pathlib.Path(args[args.index('--outdir') + 1])
doc = fitz.open()
for index in range({pages}):
    page = doc.new_page()
    page.insert_text((72, 72), f'Slide {{index + 1}}')
doc.save(out / 'source.pdf')
''', encoding="utf-8")
    binary.chmod(0o700)
    monkeypatch.setenv("OA_LIBREOFFICE_BIN", str(binary))


def test_pptx_preserves_every_slide_and_original_resource(tmp_path, monkeypatch):
    item = _presentation(tmp_path)
    before = Path(item["path"]).read_bytes()
    _converter(tmp_path, monkeypatch)
    images, manifest, contents = extractors.prepare_materials([item], out_dir=tmp_path / "out")
    assert len(images) == manifest[0]["page_count"] == 4
    assert manifest[0]["status"] == "pdf_rendered"
    assert all(image["resource_id"] == "pptx-source" for image in images)
    assert all(image["source_content_sha256"] == hashlib.sha256(before).hexdigest() for image in images)
    assert all(Path(image["path"]).is_file() for image in images)
    assert len(contents[0]["key_text"]) == 4
    assert Path(item["path"]).read_bytes() == before
    assert not any(path.is_dir() for path in (tmp_path / "out").glob("pptx-*"))


@pytest.mark.parametrize("kind", [
    "corrupt", "corrupt_deflate", "missing_converter", "failed_converter", "short_pdf",
])
def test_pptx_failures_keep_attachment_completeness_gate(tmp_path, monkeypatch, kind):
    item = _presentation(tmp_path)
    source = Path(item["path"])
    if kind == "corrupt":
        source.write_bytes(b"not a presentation")
    elif kind == "corrupt_deflate":
        with zipfile.ZipFile(source) as archive:
            member = archive.getinfo("ppt/presentation.xml")
        raw = bytearray(source.read_bytes())
        name_length, extra_length = struct.unpack_from("<HH", raw, member.header_offset + 26)
        offset = member.header_offset + 30 + name_length + extra_length
        raw[offset:offset + member.compress_size] = b"\xff" * member.compress_size
        source.write_bytes(raw)
        # The ZIP metadata is valid, but reading its XML hits a corrupt DEFLATE stream.
        with zipfile.ZipFile(source) as archive:
            assert archive.getinfo("ppt/presentation.xml").file_size > 0
    if kind == "missing_converter":
        monkeypatch.setenv("OA_LIBREOFFICE_BIN", str(tmp_path / "missing"))
    else:
        _converter(tmp_path, monkeypatch, pages=2 if kind == "short_pdf" else 4,
                   fail=kind == "failed_converter")
    with pytest.raises(RuntimeError, match="^attachment_content_incomplete$"):
        extractors.prepare_materials([item], out_dir=tmp_path / "out")


def test_pptx_conversion_timeout_remains_incomplete(tmp_path, monkeypatch):
    item = _presentation(tmp_path)
    binary = tmp_path / "soffice"
    binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    binary.chmod(0o700)
    monkeypatch.setenv("OA_LIBREOFFICE_BIN", str(binary))
    monkeypatch.setattr(extractors, "LEGACY_DOC_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(RuntimeError, match="^attachment_content_incomplete$"):
        extractors.prepare_materials([item], out_dir=tmp_path / "out")
    assert not any(path.is_dir() for path in (tmp_path / "out").glob("pptx-*"))
