from io import BytesIO
from pathlib import Path

import pytest

from apps.oa_approval.material_store import MaterialStore


def test_streaming_download_stops_before_exceeding_shared_budget(tmp_path):
    store = MaterialStore(tmp_path / 'private', limit_bytes=10)
    existing = tmp_path / 'legacy' / 'invoice.pdf'
    store.write_bytes(existing, b'123456', source={'request_id': '101', 'resource_id': '9'})
    target = tmp_path / 'worktree' / 'new.pdf'
    with pytest.raises(ValueError, match='material_budget_exceeded'):
        store.copy_stream(target, BytesIO(b'12345'))
    assert existing.read_bytes() == b'123456'
    assert not target.exists()
    assert store.report()['used_bytes'] == 6


def test_closed_copies_are_retired_with_original_location_and_protected_evidence_survives(tmp_path):
    store = MaterialStore(tmp_path / 'private', limit_bytes=10)
    closed = tmp_path / 'legacy' / 'old.pdf'
    store.write_bytes(closed, b'123456', source={'request_id': '101', 'resource_id': '9'})
    store.mark_disposable(closed, closed=True, write_state='confirmed_no_write')
    target = tmp_path / 'new.pdf'
    store.write_bytes(target, b'12345')
    assert not closed.exists()
    assert target.read_bytes() == b'12345'
    history = store.retired_materials()
    assert history[0]['path'] == str(closed)
    assert history[0]['source'] == {'request_id': '101', 'resource_id': '9'}
    assert history[0]['sha256'] == '8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92'
    assert history[0]['status'] == 'completed'


def test_inventory_counts_unregistered_files_in_adopted_legacy_directory(tmp_path):
    store = MaterialStore(tmp_path / 'private', limit_bytes=10)
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    (legacy / 'first').write_bytes(b'1234')
    store.adopt_directory(legacy, source={'flow_key': 'general_expense'})
    (legacy / 'later').write_bytes(b'123456')
    assert store.report()['used_bytes'] == 10
    with pytest.raises(ValueError, match='material_budget_exceeded'):
        store.write_bytes(tmp_path / 'new', b'x')


@pytest.mark.parametrize('has_active_attempt', [False, True])
def test_capacity_reclaims_closed_attempt_attachments_but_keeps_active_materials_and_receipts(tmp_path, has_active_attempt):
    import hashlib
    import json

    received_at = '2026-10-20T00:00:00+00:00'
    case = hashlib.sha256(f'101\x1f{received_at}'.encode()).hexdigest()
    root = tmp_path / 'general_expense' / 'cases' / case
    older = root / 'attempt-1'
    current = root / 'attempt-2'
    store = MaterialStore(tmp_path / 'private', limit_bytes=10)
    attempts = [(1, older, b'123456')]
    if has_active_attempt:
        attempts.append((2, current, b'12'))
    for attempt, directory, body in attempts:
        material = directory / 'attachments' / 'invoice.pdf'
        store.write_bytes(material, body, source={'request_id': '101', 'resource_id': str(attempt)})
        (directory / 'state.json').write_text(json.dumps({
            'schema_version': 1, 'kind': 'preaudit_action',
            'execution_state': 'closed' if attempt == 1 else 'review_required',
            'business_state': 'pass', 'write_state': 'submitted', 'workflow_key': 'general_expense',
            'case_key': case, 'attempt': attempt, 'request_id': '101', 'received_at': received_at,
            'created_at': received_at,
        }))
    receipt = older / 'receipt.json'
    receipt.write_bytes(b'{}')
    store.adopt_files([receipt])
    store.write_bytes(tmp_path / 'next', b'12345')
    assert not (older / 'attachments' / 'invoice.pdf').exists()
    if has_active_attempt:
        assert (current / 'attachments' / 'invoice.pdf').exists()
    assert receipt.read_bytes() == b'{}'
    with pytest.raises(ValueError, match='material_retired'):
        store.require_available(older / 'attachments' / 'invoice.pdf')
    assert store.retired_materials()[0]['source']['request_id'] == '101'


@pytest.mark.parametrize('location', ['git_scratch', 'nested_git_runtime', 'git_runtime', 'managed_experiment'])
def test_closed_state_cannot_make_source_or_unfinished_experiment_disposable(tmp_path, location):
    import hashlib
    import json
    import subprocess

    store = MaterialStore(tmp_path / 'private', limit_bytes=4096)
    if location != 'managed_experiment':
        checkout = tmp_path / 'oa-workspace-dev'
        subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
        if location == 'nested_git_runtime':
            checkout = checkout / '.scratch/nested-checkout'
            subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
        root = checkout / ('.scratch/fixture' if location == 'git_scratch' else 'runtime')
    else:
        root = store.root / 'experiments/trial'
    received_at = '2026-10-20T00:00:00+00:00'
    case = hashlib.sha256(f'101\x1f{received_at}'.encode()).hexdigest()
    attempt = root / 'general_expense/cases' / case / 'attempt-1'
    material = attempt / 'attachments/original.pdf'
    material.parent.mkdir(parents=True)
    material.write_bytes(b'123456')
    (attempt / 'state.json').write_text(json.dumps({
        'schema_version': 1, 'kind': 'preaudit_action', 'execution_state': 'closed',
        'business_state': 'pass', 'write_state': 'submitted', 'workflow_key': 'general_expense',
        'case_key': case, 'attempt': 1, 'request_id': '101', 'received_at': received_at, 'created_at': received_at,
    }))
    store.adopt_directory(root, source={'kind': 'legacy'})
    used = store.report()['used_bytes']
    result = store.reclaim(required_bytes=4096 - used + 1)
    assert result['freed_bytes'] == (6 if location == 'git_runtime' else 0)
    if location != 'git_runtime':
        assert material.read_bytes() == b'123456'
    if location == 'managed_experiment':
        store.mark_disposable(material, closed=True, write_state='not_applicable')
        result = store.reclaim(required_bytes=4096 - used + 1)
        assert result['freed_bytes'] == 6


def test_status_does_not_create_inventory(tmp_path):
    store = MaterialStore(tmp_path / 'private')
    assert store.report()['used_bytes'] == 0
    assert not store.root.exists()


def test_experiment_only_allows_admitted_imports_and_seals_files_after_failure(tmp_path):
    store = MaterialStore(tmp_path / 'private', limit_bytes=8)
    directory = store.managed_directory('trial')
    with pytest.raises(PermissionError):
        (directory / 'unmanaged').write_bytes(b'123456789')
    store.write_bytes(directory / 'admitted', b'12345678')
    with pytest.raises(PermissionError):
        (directory / 'admitted').write_bytes(b'changed')
    with pytest.raises(ValueError, match='material_budget_exceeded'):
        store.write_bytes(directory / 'over-budget', b'x')
    with pytest.raises(PermissionError):
        (directory / 'still-sealed').write_bytes(b'x')
    assert store.report()['used_bytes'] == 8


def test_reusing_finished_experiment_reclaims_old_copy_then_reseals_directory(tmp_path):
    store = MaterialStore(tmp_path / 'private', limit_bytes=8)
    directory = store.managed_directory('trial')
    store.write_bytes(directory / 'old', b'1234')
    store.mark_disposable(directory / 'old', closed=True, write_state='not_applicable')
    store.write_bytes(directory / 'new', b'12345')
    assert not (directory / 'old').exists()
    assert (directory / 'new').read_bytes() == b'12345'
    with pytest.raises(PermissionError):
        (directory / 'bypass').write_bytes(b'large')


@pytest.mark.parametrize('recovery', ['report', 'cli_status'])
def test_interrupted_experiment_import_is_resealed_and_partial_payload_stays_protected(tmp_path, monkeypatch, capsys, recovery):
    import subprocess
    import sys
    from apps.oa_approval import material_store
    from scripts import project_space

    monkeypatch.setattr(material_store, 'DEFAULT_LIMIT_BYTES', 8)
    store = MaterialStore()
    directory = store.managed_directory('trial')
    program = '''
from pathlib import Path
import os, sys
from apps.oa_approval import material_store
material_store.discover_project = lambda root: {'checkouts': [], 'scopes': {}, 'files': set()}
class Interrupted:
    calls = 0
    def read(self, size):
        self.calls += 1
        if self.calls == 1:
            return b'1234'
        os._exit(2)
material_store.MaterialStore(Path(sys.argv[1]), limit_bytes=8).copy_stream(Path(sys.argv[2]), Interrupted())
'''
    result = subprocess.run([sys.executable, '-B', '-c', program, str(store.root), str(directory / 'unfinished')], timeout=10)
    assert result.returncode == 2
    if recovery == 'cli_status':
        root = tmp_path / 'oa-workspace-dev'
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        assert project_space.main(['--root', str(root), 'status']) == 0
        capsys.readouterr()
    report = store.report()
    assert report['used_bytes'] == 4
    assert report['protected_bytes'] == report['used_bytes']
    assert any(entry['status'] == 'protected' and entry['bytes'] == 4 for entry in store.registered_files())
    assert not (directory / 'unfinished').exists()
    with pytest.raises(PermissionError):
        (directory / 'bypass').write_bytes(b'123456789')


def test_two_processes_cannot_spend_same_capacity(tmp_path):
    import subprocess
    import sys

    program = '''
from pathlib import Path
import sys
from apps.oa_approval import material_store
material_store.discover_project = lambda root: {'checkouts': [], 'scopes': {}, 'files': set()}
from apps.oa_approval.material_store import MaterialStore
try:
    MaterialStore(Path(sys.argv[1]), limit_bytes=10).write_bytes(Path(sys.argv[2]), b'123456')
    print('complete')
except ValueError as error:
    print(error)
'''
    processes = [subprocess.Popen([sys.executable, '-B', '-c', program, str(tmp_path / 'private'),
                                   str(tmp_path / f'writer-{index}')], stdout=subprocess.PIPE, text=True)
                 for index in range(2)]
    results = sorted(process.communicate(timeout=15)[0].strip() for process in processes)
    assert results == ['complete', 'material_budget_exceeded']
    assert MaterialStore(tmp_path / 'private', limit_bytes=10).report()['used_bytes'] == 6


def test_crashed_external_reservation_releases_only_unused_capacity(tmp_path):
    import subprocess
    import sys

    program = '''
from pathlib import Path
import os, sys
from apps.oa_approval import material_store
material_store.discover_project = lambda root: {'checkouts': [], 'scopes': {}, 'files': set()}
from apps.oa_approval.material_store import MaterialStore
with MaterialStore(Path(sys.argv[1]), limit_bytes=10).reserved_file(Path(sys.argv[2]), max_bytes=10) as path:
    path.write_bytes(b'1234')
    os._exit(2)
'''
    output = tmp_path / 'interrupted'
    result = subprocess.run([sys.executable, '-B', '-c', program, str(tmp_path / 'private'), str(output)])
    assert result.returncode == 2
    store = MaterialStore(tmp_path / 'private', limit_bytes=10)
    store.write_bytes(tmp_path / 'new', b'123456')
    assert output.read_bytes() == b'1234'
    assert store.report()['used_bytes'] == 10


def test_active_material_reader_prevents_capacity_eviction(tmp_path):
    store = MaterialStore(tmp_path / 'private', limit_bytes=10)
    original = tmp_path / 'original'
    store.write_bytes(original, b'123456')
    store.mark_disposable(original, closed=True, write_state='not_applicable')
    with store.read_lease([original]):
        with pytest.raises(ValueError, match='material_budget_exceeded'):
            store.write_bytes(tmp_path / 'next', b'12345')
        assert original.read_bytes() == b'123456'
    store.write_bytes(tmp_path / 'next', b'12345')
    assert not original.exists()


def test_symlink_and_reopened_attempt_are_never_evicted(tmp_path):
    import json

    store = MaterialStore(tmp_path / 'private', limit_bytes=10)
    attempt = tmp_path / 'attempt-1'
    material = attempt / 'attachments' / 'source'
    store.write_bytes(material, b'123456')
    (attempt / 'action.json').write_text('{}')
    (attempt / 'state.json').write_text(json.dumps({'execution_state': 'closed', 'write_state': 'submitted'}))
    store.mark_disposable(material, closed=True, write_state='submitted')
    (attempt / 'state.json').write_text(json.dumps({'execution_state': 'review_required', 'write_state': 'unknown'}))
    with pytest.raises(ValueError, match='material_budget_exceeded'):
        store.write_bytes(tmp_path / 'new', b'12345')
    assert material.exists()
    alias = tmp_path / 'alias'
    alias.symlink_to(attempt, target_is_directory=True)
    with pytest.raises(ValueError, match='material_path_untrusted'):
        store.write_bytes(alias / 'outside', b'1')


def test_public_download_stream_and_pdf_preparation_share_budget(tmp_path, monkeypatch):
    import fitz
    from apps.oa_approval import attachment_extractors, attachments, material_store

    with fitz.open() as document:
        document.new_page().insert_text((20, 20), 'Synthetic receipt')
        body = document.tobytes()
    monkeypatch.setattr(material_store, 'DEFAULT_LIMIT_BYTES', len(body) + 10)
    monkeypatch.setattr(attachments, 'OA_HOST', 'https://synthetic.example')
    monkeypatch.setattr(attachments, 'EXTRA_DOWNLOAD_HOSTS', {'synthetic.example'})
    monkeypatch.setattr(attachments.safe_http, 'urlopen', lambda *_a, **_k: BytesIO(body))
    files, errors = attachments.download_resources([
        {'id': '4', 'name': 'receipt.pdf', 'downloadUrl': 'https://synthetic.example/receipt'}],
        dest_dir=tmp_path / 'downloads', timeout_sec=2, allow_browser_download=True,
        request_id='101', flow_key='general_expense')
    assert errors == []
    assert len(files) == 1
    with pytest.raises(ValueError, match='material_budget_exceeded'):
        attachment_extractors.prepare_materials(files, out_dir=tmp_path / 'prepared')
    assert Path(files[0]['path']).read_bytes() == body
    assert material_store.MaterialStore().report()['used_bytes'] == len(body)


def test_archive_expansion_is_admitted_before_member_file_write(tmp_path, monkeypatch):
    import zipfile
    from apps.oa_approval import attachments, material_store

    archive = tmp_path / 'sample.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('large.txt', b'x' * 11)
    monkeypatch.setattr(material_store, 'DEFAULT_LIMIT_BYTES', 10)
    files, error = attachments.extract_zip_safely(archive, dest_dir=tmp_path / 'downloads',
                                                resource={'id': '9', 'name': 'sample.zip'})
    assert files == []
    assert 'material_budget_exceeded' in error
    assert not list((tmp_path / 'downloads').rglob('large.txt'))
