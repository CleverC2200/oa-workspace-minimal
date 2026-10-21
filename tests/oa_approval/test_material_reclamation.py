from __future__ import annotations

import errno
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from apps.oa_approval.material_store import MaterialStore


def _copies(tmp_path: Path, count: int):
    store = MaterialStore(tmp_path / 'private', limit_bytes=4096)
    paths = []
    for index in range(count):
        path = tmp_path / 'copies' / str(index) / 'original.pdf'
        store.write_bytes(path, b'1234', source={'request_id': 'recorded-case', 'resource_id': f'copy-{index}'})
        store.mark_disposable(path, closed=True, write_state='not_applicable')
        paths.append(path)
    protected = tmp_path / 'protected.pdf'
    store.write_bytes(protected, b'keep')
    return store, paths, protected


@pytest.mark.parametrize('count', [1, 40])
def test_reclamation_checkpoints_a_batch_without_rewriting_inventory_for_each_file(tmp_path, monkeypatch, count):
    store, paths, protected = _copies(tmp_path, count)
    writes = []
    replace = os.replace

    def record_replace(source, target, *args, **kwargs):
        if Path(target) == store.root / 'inventory.json':
            writes.append(Path(source).stat().st_size)
        return replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, 'replace', record_replace)
    report = store.reclaim(required_bytes=4092)
    assert report['freed_bytes'] == 4 * count
    assert all(not path.exists() for path in paths)
    assert protected.read_bytes() == b'keep'
    assert len(writes) == 2
    assert sum(writes) < 3 * (store.root / 'inventory.json').stat().st_size


def test_unlink_failure_checkpoints_completed_files_and_leaves_remaining_intents_planned(tmp_path, monkeypatch):
    store, paths, protected = _copies(tmp_path, 3)
    unlink = Path.unlink

    def fail_second(path, *args, **kwargs):
        if path == paths[1]:
            raise OSError(errno.EIO, 'synthetic unlink failure')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', fail_second)
    with pytest.raises(OSError, match='synthetic unlink failure'):
        store.reclaim(required_bytes=4092)
    events = {event['path']: event for event in store.retired_materials()}
    assert events[str(paths[0])]['status'] == 'completed'
    assert [events[str(path)]['status'] for path in paths[1:]] == ['planned', 'planned']
    assert all(path.read_bytes() == b'1234' for path in paths[1:])
    assert protected.read_bytes() == b'keep'


def test_hard_exit_recovers_only_deleted_files_and_keeps_other_intents_planned(tmp_path):
    store, paths, protected = _copies(tmp_path, 3)
    program = '''
from pathlib import Path
import os, sys
from apps.oa_approval.material_store import MaterialStore
original = Path.unlink
def interrupted(path, *args, **kwargs):
    original(path, *args, **kwargs)
    if path == Path(sys.argv[2]):
        os._exit(2)
Path.unlink = interrupted
MaterialStore(Path(sys.argv[1]), limit_bytes=4096).reclaim(required_bytes=4092)
'''
    result = subprocess.run([sys.executable, '-B', '-c', program, str(store.root), str(paths[0])], timeout=10)
    assert result.returncode == 2
    events = {event['path']: event for event in store.retired_materials()}
    assert events[str(paths[0])]['status'] == 'completed'
    assert events[str(paths[0])]['recovered'] is True
    assert [events[str(path)]['status'] for path in paths[1:]] == ['planned', 'planned']
    assert protected.read_bytes() == b'keep'
    assert store.report()['used_bytes'] == 12
    resumed = store.reclaim(required_bytes=4092)
    assert resumed['freed_bytes'] == 8
    events = store.retired_materials()
    assert len(events) == 3
    assert all(event['status'] == 'completed' for event in events)
    assert store.report()['used_bytes'] == 4


@pytest.mark.parametrize('change', ['content', 'state', 'missing'])
def test_planned_material_is_rechecked_and_changed_or_unavailable_files_are_not_claimed_deleted(tmp_path, monkeypatch, change):
    store, paths, protected = _copies(tmp_path, 3)
    replace = os.replace
    changed = False

    def change_after_plan(source, target, *args, **kwargs):
        nonlocal changed
        result = replace(source, target, *args, **kwargs)
        if Path(target) == store.root / 'inventory.json' and not changed:
            changed = True
            if change == 'content':
                paths[1].write_bytes(b'changed')
            elif change == 'state':
                (paths[1].parent / 'state.json').write_text('{}')
            else:
                paths[1].unlink()
        return result

    monkeypatch.setattr(os, 'replace', change_after_plan)
    report = store.reclaim(required_bytes=4092)
    assert report['freed_bytes'] == 8
    event = next(event for event in store.retired_materials() if event['path'] == str(paths[1]))
    assert event['status'] == 'skipped'
    assert event['source'] == {'request_id': 'recorded-case', 'resource_id': 'copy-1'}
    assert event['sha256'] == '03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4'
    if change != 'missing':
        assert paths[1].exists()
        entry = next(entry for entry in store.registered_files() if entry['path'] == str(paths[1]))
        assert entry['status'] == 'protected'
    assert protected.read_bytes() == b'keep'


def test_streaming_admission_counts_protected_content_growth_found_during_reclamation(tmp_path, monkeypatch):
    store, paths, protected = _copies(tmp_path, 3)
    replace = os.replace
    changed = False

    def change_after_plan(source, target, *args, **kwargs):
        nonlocal changed
        if Path(target) == store.root / 'inventory.json' and not changed:
            snapshot = json.loads(Path(source).read_text())
            if any(event['status'] == 'planned' for event in snapshot['events']):
                changed = True
                paths[1].write_bytes(b'changed')
        return replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, 'replace', change_after_plan)
    target = tmp_path / 'incoming'
    with pytest.raises(ValueError, match='material_budget_exceeded'):
        store.copy_stream(target, BytesIO(b'x' * 4086))
    assert not target.exists()
    assert paths[1].read_bytes() == b'changed'
    assert protected.read_bytes() == b'keep'
    assert store.report()['used_bytes'] == 11
