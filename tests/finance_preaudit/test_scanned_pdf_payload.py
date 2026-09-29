import hashlib
import random
from pathlib import Path

import fitz

from apps.oa_approval import attachment_extractors


def _scanned_pdf(path: Path, pages: int = 3) -> None:
    # Scan-like texture plus text: PNG inflates, while dimensions must survive.
    samples = random.Random(7).randbytes(600 * 840 * 3)
    scan = fitz.Pixmap(fitz.csRGB, 600, 840, samples, 0)
    with fitz.open() as document:
        for index in range(pages):
            page = document.new_page(width=300, height=420)
            page.insert_image(page.rect, stream=scan.tobytes("jpeg", jpg_quality=75))
            page.insert_text((20, 30), f"Page {index + 1}: invoice 12345 / 678.90", fontsize=10)
        document.save(path)


def test_compact_pdf_keeps_all_pages_dimensions_ownership_and_source(tmp_path):
    source = tmp_path / "scan.pdf"
    _scanned_pdf(source)
    original = source.read_bytes()
    descriptor = {"path": str(source), "resource_id": "owned-resource", "name": "scan.pdf"}
    old, _, _ = attachment_extractors.prepare_materials([descriptor], out_dir=tmp_path / "old")
    compact, manifest, text = attachment_extractors.prepare_materials(
        [descriptor], out_dir=tmp_path / "compact", compact_pdf=True,
    )
    assert source.read_bytes() == original
    assert len(compact) == len(old) == manifest[0]["page_count"] == 3
    assert manifest[0]["image_count"] == 3
    assert sum(Path(p["path"]).stat().st_size for p in compact) < sum(
        Path(p["path"]).stat().st_size for p in old
    ) * 0.7
    for index, (before, after) in enumerate(zip(old, compact), 1):
        assert after["resource_id"] == "owned-resource"
        assert after["kind"] == f"pdf_page_{index}"
        assert after["source_content_sha256"] == hashlib.sha256(original).hexdigest()
        path = Path(after["path"])
        assert path.suffix == ".jpg"
        assert after["content_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        first, second = fitz.Pixmap(str(before["path"])), fitz.Pixmap(str(path))
        assert (first.width, first.height) == (second.width, second.height) == (600, 840)
    assert len(text) == 1
    assert all(f"Page {index}" in str(text) for index in range(1, 4))


def test_compact_pdf_keeps_small_text_pages_lossless(tmp_path):
    source = tmp_path / "text.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((20, 30), "Invoice 12345 / 678.90")
        document.save(source)
    images, _, _ = attachment_extractors.prepare_materials(
        [{"path": str(source), "resource_id": "text"}],
        out_dir=tmp_path / "prepared", compact_pdf=True,
    )
    assert Path(images[0]["path"]).suffix == ".png"


def test_default_pdf_preparation_remains_lossless(tmp_path):
    source = tmp_path / "scan.pdf"
    _scanned_pdf(source, pages=1)
    images, _, _ = attachment_extractors.prepare_materials(
        [{"path": str(source), "resource_id": "packaging"}], out_dir=tmp_path / "prepared",
    )
    assert Path(images[0]["path"]).suffix == ".png"
