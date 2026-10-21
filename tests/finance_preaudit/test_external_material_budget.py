from __future__ import annotations

from pathlib import Path
import sys

import pytest

from apps.oa_approval import attachment_extractors, material_store

pytestmark = pytest.mark.usefixtures('recorded_bounded_office_workspace')

def _office(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> dict:
    binary = tmp_path / 'soffice'
    binary.write_text(f'#!{sys.executable}\n' + body)
    binary.chmod(0o700)
    monkeypatch.setenv('OA_LIBREOFFICE_BIN', str(binary))
    source = tmp_path / 'document.doc'
    source.write_bytes(b'synthetic document')
    return {'resource_id': 'document', 'path': str(source), 'name': source.name}


def test_office_rejects_capacity_shortage_before_converter_starts(tmp_path, monkeypatch):
    started = tmp_path / 'started'
    item = _office(tmp_path, monkeypatch, f'from pathlib import Path\nPath({str(started)!r}).touch()\n')
    monkeypatch.setattr(material_store, 'DEFAULT_LIMIT_BYTES', 1024)
    with pytest.raises(ValueError, match='material_budget_exceeded'):
        attachment_extractors.prepare_materials([item], out_dir=tmp_path / 'out')
    assert not started.exists()
    assert not list((tmp_path / 'out').glob('legacy-doc-*'))
    assert material_store.MaterialStore().report()['used_bytes'] <= 1024


def test_office_external_payload_output_has_hard_file_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(attachment_extractors, 'EXTERNAL_PAYLOAD_MAX_BYTES', 4096)
    limits = tmp_path / 'child-limit'
    item = _office(tmp_path, monkeypatch, f'''from pathlib import Path
import resource
import sys
out = Path(sys.argv[sys.argv.index('--outdir') + 1])
soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
Path({str(limits)!r}).write_text(str(soft))
assert 0 < soft <= 200_000_000 and soft == hard
(out / 'source.pdf').write_bytes(b'x' * (soft + 1))
''')
    with pytest.raises(RuntimeError, match='legacy_doc_conversion_failed'):
        attachment_extractors.prepare_materials([item], out_dir=tmp_path / 'out')
    assert not list((tmp_path / 'out').glob('legacy-doc-*'))
    assert limits.read_text() == '4096'
    assert material_store.MaterialStore().report()['used_bytes'] == 18
