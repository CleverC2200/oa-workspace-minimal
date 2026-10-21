from pathlib import Path
from types import SimpleNamespace
import io
import zipfile

import fitz
import pytest

from apps.oa_approval import attachments, attachment_extractors


def _rar_transport(monkeypatch, entries, *, tool="/usr/bin/bsdtar", omit=(), declared_size=4):
    monkeypatch.setattr(attachments, "_rar_tool", lambda: tool)
    extracted = []

    def run(args, **_kwargs):
        if "-tf" in args:
            output = "\n".join(name for kind, name in entries) + "\n"
        elif "-tvf" in args:
            output = "\n".join(
                f"{kind}rw-r--r-- 0 0 0 {declared_size} Jan 1 2026 {name}"
                for kind, name in entries
            ) + "\n"
        elif "l" in args:
            output = "\n\n".join(
                f"Path = {name}\nFolder = {'+' if kind == 'd' else '-'}\n"
                f"Size = {declared_size}\nAttributes = {'D' if kind == 'd' else 'A'}\n"
                + ("Symbolic Link = other\n" if kind == "l" else "")
                for kind, name in entries
            ) + "\n"
        else:
            root = Path(args[args.index("-C") + 1]) if "-C" in args else Path(
                next(arg[2:] for arg in args if arg.startswith("-o"))
            )
            extracted.append(True)
            for kind, name in entries:
                target = root / name
                if kind == "d":
                    target.mkdir(parents=True, exist_ok=True)
                elif name not in omit:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("read")
            output = ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    class StreamedExtraction:
        def __init__(self, args, **_kwargs):
            if '-tvf' in args or 'l' in args:
                self.stdout = io.BytesIO(run(args).stdout.encode())
            else:
                extracted.append(True)
                member = args[-1]
                self.stdout = io.BytesIO(b'' if member in omit else b'read')
            self.returncode = 0

        def wait(self, **_kwargs):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(attachments.subprocess, "run", run)
    monkeypatch.setattr(attachments.subprocess, "Popen", StreamedExtraction)
    return extracted


@pytest.mark.parametrize("tool", ["/usr/bin/bsdtar", "/usr/bin/7zz"])
def test_rar_directory_metadata_preserves_files_and_ownership(tmp_path, monkeypatch, tool):
    monkeypatch.setattr(attachments, "ARCHIVE_MAX_FILES", 2)
    entries = [("d", f"目录 {index}") for index in range(2)] + [
        ("-", "目录 0/付款 明细.txt"), ("-", "目录 1/receipt.txt"),
    ]
    _rar_transport(monkeypatch, entries, tool=tool)
    item, error = attachments._attach_archive_children(
        tmp_path, {"id": "source", "name": "bundle.rar"},
        {"resource_id": "source", "name": "bundle.rar", "path": str(tmp_path / "bundle.rar")},
    )
    assert error == ""
    assert item["archive_extracted_count"] == 2
    assert [child["archive_path"] for child in item["extracted_files"]] == [name for kind, name in entries if kind == "-"]
    assert all(child["parent_resource_id"] == "source" for child in item["extracted_files"])


@pytest.mark.parametrize("entries", [
    [("d", "../outside"), ("-", "leaf.txt")],
    [("l", "shortcut"), ("-", "leaf.txt")],
    [("h", "hard-link"), ("-", "leaf.txt")],
])
def test_rar_unsafe_entries_are_rejected_before_extraction(tmp_path, monkeypatch, entries):
    extracted = _rar_transport(monkeypatch, entries)
    result, error = attachments.extract_rar_safely(
        tmp_path / "bundle.rar", dest_dir=tmp_path, resource={"id": "source"},
    )
    assert result == []
    assert error
    assert extracted == []


def test_rar_missing_listed_file_is_not_silently_omitted(tmp_path, monkeypatch):
    _rar_transport(monkeypatch, [("-", "leaf.txt")], omit={"leaf.txt"})
    result, error = attachments.extract_rar_safely(
        tmp_path / "bundle.rar", dest_dir=tmp_path, resource={"id": "source"},
    )
    assert result == []
    assert error


def test_rar_explicit_file_limit_still_rejects_before_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "ARCHIVE_MAX_FILES", 2)
    extracted = _rar_transport(monkeypatch, [("-", f"leaf-{index}.txt") for index in range(3)])
    result, error = attachments.extract_rar_safely(
        tmp_path / "bundle.rar", dest_dir=tmp_path, resource={"id": "source"},
    )
    assert result == []
    assert "文件数超过限制 2" in error
    assert extracted == []


@pytest.mark.parametrize(("single_limit", "total_limit"), [(3, 100), (100, 6)])
def test_rar_size_limits_reject_before_extraction(tmp_path, monkeypatch, single_limit, total_limit):
    monkeypatch.setattr(attachments, "ARCHIVE_MAX_FILE_BYTES", single_limit)
    monkeypatch.setattr(attachments, "ARCHIVE_MAX_TOTAL_BYTES", total_limit)
    extracted = _rar_transport(monkeypatch, [("-", "a.txt"), ("-", "b.txt")])
    result, error = attachments.extract_rar_safely(
        tmp_path / "bundle.rar", dest_dir=tmp_path, resource={"id": "source"},
    )
    assert result == []
    assert "超过限制" in error
    assert extracted == []


def test_rar_directory_flood_still_has_a_member_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "ARCHIVE_MAX_FILES", 2)
    extracted = _rar_transport(monkeypatch, [("d", f"folder-{index}") for index in range(5)])
    result, error = attachments.extract_rar_safely(
        tmp_path / "bundle.rar", dest_dir=tmp_path, resource={"id": "source"},
    )
    assert result == []
    assert "成员数超过限制" in error
    assert extracted == []


def test_rar_declared_size_must_match_extracted_file(tmp_path, monkeypatch):
    _rar_transport(monkeypatch, [("-", "leaf.txt")], declared_size=5)
    result, error = attachments.extract_rar_safely(
        tmp_path / "bundle.rar", dest_dir=tmp_path, resource={"id": "source"},
    )
    assert result == []
    assert "大小不符" in error


def test_nested_archive_and_later_leaf_files_share_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "ARCHIVE_MAX_FILES", 3)
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("a.txt", "one")
        archive.writestr("b.txt", "two")
    source = tmp_path / "outer.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("nested.zip", nested.getvalue())
        archive.writestr("c.txt", "three")
        archive.writestr("d.txt", "four")
    item, error = attachments._attach_archive_children(
        tmp_path / "output", {"id": "source", "name": "outer.zip"},
        {"resource_id": "source", "path": str(source)},
    )
    assert item is None
    assert "展开后文件数超过限制 3" in error


def test_default_capacity_prepares_all_108_files_through_download_boundary(tmp_path, monkeypatch):
    archive = tmp_path / "bundle.zip"
    image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2), False).tobytes("png")
    with zipfile.ZipFile(archive, "w") as output:
        for index in range(108):
            output.writestr(f"folder/file-{index}.png", image)
    monkeypatch.setattr(attachments, "_download_resource_direct", lambda *_a, **_kw: (archive, ""))
    monkeypatch.setattr(attachments.runtime_control, "stop_requested", lambda: False)
    monkeypatch.setattr(attachments, "_record_attachment_entry", lambda *_a, **_kw: None)
    downloaded, errors = attachments.download_resources(
        [{"id": "source", "name": "bundle.zip", "downloadUrl": "/recorded/bundle.zip"}],
        dest_dir=tmp_path / "download", timeout_sec=5, allow_browser_download=True,
        request_id="recorded", flow_key="tpm_small_marketing_fee", reuse_cache=False,
    )
    assert errors == []
    assert downloaded[0]["archive_extracted_count"] == 108
    images, manifest, structured = attachment_extractors.prepare_materials(downloaded, out_dir=tmp_path / "prepared")
    assert len(images) == 108
    assert manifest[0]["archive_extracted_count"] == 108
    assert structured == []
    assert {part["parent_resource_id"] for part in images} == {"source"}
