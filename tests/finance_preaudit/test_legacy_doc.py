from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import hashlib
import fcntl
import json
import os
from pathlib import Path
import sys
import threading

import fitz
import pytest

from apps.oa_approval import attachment_extractors as extractors
from apps.oa_finance_preaudit import flow_engine

pytestmark = pytest.mark.usefixtures('recorded_bounded_office_workspace')

def _converter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    binary = tmp_path / "soffice"
    binary.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    binary.chmod(0o700)
    monkeypatch.setenv("OA_LIBREOFFICE_BIN", str(binary))
    return binary


def _item(tmp_path: Path) -> dict:
    source = tmp_path / "document.doc"
    source.write_bytes(b"synthetic legacy document")
    return {"resource_id": "doc-1", "name": source.name, "path": str(source)}


def test_legacy_doc_preserves_all_pages_and_original_identity(tmp_path, monkeypatch):
    _converter(tmp_path, monkeypatch, '''import pathlib, sys, fitz
out = pathlib.Path(sys.argv[sys.argv.index('--outdir') + 1])
doc = fitz.open()
for text in ['Page one table', 'Page two signature']:
    page = doc.new_page()
    page.insert_text((72, 72), text)
doc.save(out / 'source.pdf')
''')
    item = _item(tmp_path)
    images, manifest, contents = extractors.prepare_materials(
        [item], out_dir=tmp_path / "prepared"
    )
    assert len(images) == manifest[0]["page_count"] == 2
    assert [image["resource_id"] for image in images] == ["doc-1", "doc-1"]
    original_hash = hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()
    assert all(image["source_content_sha256"] == original_hash for image in images)
    assert all(Path(image["path"]).is_file() for image in images)
    assert "Page one table" in contents[0]["key_text"][0]
    assert "Page two signature" in contents[0]["key_text"][1]
    assert not list((tmp_path / "prepared").glob("legacy-doc-*"))


@pytest.mark.parametrize("body", [
    "raise SystemExit(1)\n", "pass\n",
    "import pathlib, sys\npathlib.Path(sys.argv[sys.argv.index('--outdir')+1], 'source.pdf').write_bytes(b'invalid')\n",
])
def test_legacy_doc_conversion_failure_is_sanitized(tmp_path, monkeypatch, body):
    _converter(tmp_path, monkeypatch, body)
    with pytest.raises(RuntimeError, match="^legacy_doc_conversion_failed$"):
        extractors.prepare_materials([_item(tmp_path)], out_dir=tmp_path / "out")


def test_legacy_doc_missing_converter_has_specific_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("OA_LIBREOFFICE_BIN", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="^legacy_doc_converter_unavailable$"):
        extractors.prepare_materials([_item(tmp_path)], out_dir=tmp_path / "out")


def test_legacy_doc_timeout_is_bounded(tmp_path, monkeypatch):
    _converter(tmp_path, monkeypatch, "import time\ntime.sleep(30)\n")
    monkeypatch.setattr(extractors, "LEGACY_DOC_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(RuntimeError, match="^legacy_doc_conversion_timeout$"):
        extractors.prepare_materials([_item(tmp_path)], out_dir=tmp_path / "out")
    assert not list((tmp_path / "out").glob("legacy-doc-*"))


def _run_converter_reap_case(tmp_path, monkeypatch, timeout):
    lock_file = tmp_path / "child.lock"
    ready = tmp_path / "child.ready"
    with fitz.open() as document:
        document.new_page()
        pdf_bytes = document.tobytes()
    child = (
        "import fcntl, pathlib, time; "
        f"f=open({str(lock_file)!r},'w'); fcntl.flock(f,fcntl.LOCK_EX); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
    )
    script = f'''import os, pathlib, subprocess, sys, time
from urllib.parse import urlparse, unquote
profile_arg = next(a for a in sys.argv if a.startswith('-env:UserInstallation='))
profile = pathlib.Path(unquote(urlparse(profile_arg.split('=', 1)[1]).path))
assert '<value>3</value>' in (profile/'registrymodifications.xcu').read_text()
out = pathlib.Path(sys.argv[sys.argv.index('--outdir') + 1])
assert profile.parent == out
if sys.platform == 'darwin':
    fontconfig = pathlib.Path(os.environ['FONTCONFIG_FILE'])
    assert fontconfig.parent == out
    assert '/System/Library/Fonts/Supplemental' in fontconfig.read_text()
    assert str(out/'font-cache') in fontconfig.read_text()
p = subprocess.Popen([sys.executable, '-c', {child!r}])
pathlib.Path({str(tmp_path / 'pid')!r}).write_text(str(p.pid))
while not pathlib.Path({str(ready)!r}).exists(): time.sleep(.01)
if {timeout!r}: time.sleep(30)
(out/'source.pdf').write_bytes({pdf_bytes!r})
'''
    _converter(tmp_path, monkeypatch, script)
    monkeypatch.delenv("FONTCONFIG_FILE", raising=False)
    monkeypatch.setattr(extractors, "LEGACY_DOC_TIMEOUT_SECONDS", 2)
    try:
        if timeout:
            with pytest.raises(RuntimeError, match="^legacy_doc_conversion_timeout$"):
                extractors.prepare_materials([_item(tmp_path)], out_dir=tmp_path / "out")
        else:
            extractors.prepare_materials([_item(tmp_path)], out_dir=tmp_path / "out")
        assert ready.exists()
        with lock_file.open() as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not list((tmp_path / "out").glob("legacy-doc-*"))
    finally:
        pid_file = tmp_path / "pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("timeout", [False, True])
def test_converter_child_reaped_and_profile_isolated(tmp_path, monkeypatch, timeout):
    _run_converter_reap_case(tmp_path, monkeypatch, timeout)


def test_converter_waits_for_child_after_parent_exit(tmp_path, monkeypatch):
    parent_killed = threading.Event()

    def defer_child_exit(pgid, signum):
        os.kill(pgid, signum)
        parent_killed.set()

    monkeypatch.setattr(os, "killpg", defer_child_exit)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_run_converter_reap_case, tmp_path, monkeypatch, True)
        try:
            assert parent_killed.wait(timeout=5)
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.1)
        finally:
            pid_file = tmp_path / "pid"
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), 9)
                except ProcessLookupError:
                    pass
        future.result(timeout=5)


@pytest.mark.parametrize("timeout", [False, True])
def test_converter_status_failure_preserves_conversion_failure(tmp_path, monkeypatch, timeout):
    with fitz.open() as document:
        document.new_page()
        pdf_bytes = document.tobytes()
    body = "import time\ntime.sleep(30)\n" if timeout else f'''import pathlib, sys
out = pathlib.Path(sys.argv[sys.argv.index('--outdir') + 1])
(out/'source.pdf').write_bytes({pdf_bytes!r})
'''
    _converter(tmp_path, monkeypatch, body)
    monkeypatch.setattr(extractors, "LEGACY_DOC_TIMEOUT_SECONDS", 0.1 if timeout else 2)
    status_calls = []

    def unavailable_status(*args, **kwargs):
        status_calls.append(args)
        raise OSError("synthetic process status unavailable")

    monkeypatch.setattr(extractors.subprocess, "run", unavailable_status)
    code = "legacy_doc_conversion_timeout" if timeout else "legacy_doc_conversion_failed"
    with pytest.raises(RuntimeError, match=f"^{code}$"):
        extractors.prepare_materials([_item(tmp_path)], out_dir=tmp_path / "out")
    assert status_calls


@pytest.mark.parametrize("code", [
    "legacy_doc_converter_unavailable", "legacy_doc_conversion_failed",
    "legacy_doc_conversion_timeout", "attachment_content_incomplete",
])
def test_collection_gap_retains_safe_failure_code(tmp_path, code):
    def collect(_request_id, _attempt):
        raise RuntimeError(code)

    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda key: [{"request_id": "sample", "received_at": "2026-01-01 00:00:00", "workflow_key": key}],
        collect_material=collect,
    )
    action = engine.next_action("small_marketing_fee")
    assert action["action"] == "needs_admin"
    assert action["reason"] == "material_collection_failed"
    assert action["failure_detail_code"] == code
    assert not Path(action["action_file"]).with_name("review-packet.json").exists()
