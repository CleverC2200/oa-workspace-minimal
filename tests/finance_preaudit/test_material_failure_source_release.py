from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import pytest

from apps.oa_finance_preaudit import flow_engine, maintenance_runtime, production_config, production_release, reconciliation
from tests.finance_preaudit.test_flow_engine_material_gaps import _engine
from tests.finance_preaudit.test_maintenance_runtime import live_snapshot
from tests.finance_preaudit.test_production_release import git, production, stopped_production


@pytest.fixture
def failed_install(stopped_production, monkeypatch):
    root, remote, revision, stop, automation, original, report = stopped_production
    stop.unlink()
    source = Path(production_release.__file__).resolve().parents[2]
    for directory in ('apps', 'scripts'):
        shutil.copytree(source / directory, root / directory, ignore=shutil.ignore_patterns('__pycache__'))
    (root / '.git/info/exclude').write_text('apps/\nscripts/\n.venv/\n')
    interpreter = root / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('#!/bin/sh\nexec "' + sys.executable + '" "$@"\n')
    interpreter.chmod(0o700)
    auth = root.parent / 'empty-auth.env'
    auth.write_text('')
    auth.chmod(0o600)
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(auth))
    monkeypatch.delenv('OA_FINANCE_PREAUDIT_WRITE_POLICY', raising=False)
    flows = root / 'runtime/finance_preaudit_flows'
    engine = _engine(flows, 'general_expense', RuntimeError('oa_session_expired'), controlled=True, submissions=[])
    action = Path(engine.next_action('general_expense', mode='controlled_write')['action_file'])
    state = json.loads(action.with_name('state.json').read_text())
    policy = root / 'runtime/control/write-policy.json'
    policy.write_text(json.dumps({'schema_version': 1, 'kind': 'preaudit_write_policy',
        'authorized_workflows': [*production_config.FINANCE_WORKFLOWS, 'packaging_layout_approval']}))
    policy.chmod(0o600)
    pending = [{'request_id': state['request_id'], 'received_at': state['received_at'], 'workflow_key': 'general_expense'}]
    def live(_target):
        snapshot = live_snapshot()
        snapshot['reconciliation'] = reconciliation.reconcile(flows, pending_rows=pending, handled_rows=[])
        path = root / ('runtime/status-' + uuid4().hex + '.json')
        raw = json.dumps(snapshot).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        receipt = {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        snapshot['maintenance_status_file'] = receipt
        proof = {'kind': 'preaudit_material_failure_source_only', 'target_root': str(root), 'actions': [str(action)]}
        snapshot['maintenance_current_inputs'] = maintenance_runtime.read_current_inputs(root, receipt, source_only_proof=proof)
        return snapshot
    live.pending_rows = pending
    return root, remote, revision, automation, original, report, action, live


def advance_source(root, remote):
    source = remote.parent / 'new-source'
    git(remote.parent, 'clone', '-q', str(remote), str(source))
    (source / 'source.txt').write_text('second')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'second')
    git(source, 'push', '-q')
    git(root, 'fetch', '-q', 'origin')
    return git(source, 'rev-parse', 'HEAD')


def test_source_repair_preserves_real_controller_failure_and_all_original_files(failed_install):
    root, remote, revision, automation, original, report, action, live = failed_install
    before = {p: p.read_bytes() for p in action.parent.rglob('*') if p.is_file()}
    cron = automation.file.read_bytes()
    expected = advance_source(root, remote)
    result = production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
        confirmed_sync=True, automation=automation, original_execution=original,
        failed_material_actions=(action,), check_original=lambda *_: report, check_live=live,
        check_configuration=lambda *_: [])
    assert result['status'] == 'synced' and result['material_failure_source_only'] is True
    assert result['before_revision'] == revision and result['after_revision'] == expected
    assert result['source_sync_state'] == 'updated'
    assert result['next_process_ready'] is False and result['approval_recovery_allowed'] is False
    assert result['original_evidence_preserved'] is True and result['write_state'] == 'not_requested'
    assert result['continuation']['blocking_reasons'] == {'pending_failure_not_isolated': 1}
    assert result['pre_sync_current_input_proof']['verified_material_failure_actions'] == [str(action)]
    assert {p: p.read_bytes() for p in action.parent.rglob('*') if p.is_file()} == before
    assert automation.file.read_bytes() == cron


@pytest.mark.parametrize('damage', ['writer-boundary.json', 'execution-receipt.json', 'review-decision.json',
    'remark.txt', 'submission-envelope.json', 'bad_receipt', 'state_identity', 'additional_block'])
def test_source_repair_rejects_untrusted_or_unexplained_failure(failed_install, damage):
    root, _, _, _, _, _, action, live = failed_install
    if damage == 'bad_receipt': action.with_name('manual-review-receipt.json').write_text('{}')
    elif damage == 'state_identity':
        state = json.loads(action.with_name('state.json').read_text())
        state['attempt'] += 1
        action.with_name('state.json').write_text(json.dumps(state))
    elif damage == 'additional_block':
        candidate = {'request_id': 'another-failure', 'received_at': '2026-10-10T10:00:00+08:00', 'workflow_key': 'general_expense'}
        def fail(*_): raise ValueError('oa_session_expired')
        engine = flow_engine.FlowEngine(root=root / 'runtime/finance_preaudit_flows',
            discover_candidates=lambda _: [candidate], collect_material=fail)
        engine.next_action('general_expense')
        live.pending_rows.append(candidate)
    else: action.with_name(damage).write_text('{}')
    with pytest.raises((RuntimeError, ValueError)): live(root)


def test_source_repair_rejects_changed_preserved_files_after_actual_source_update(failed_install):
    root, remote, before, automation, original, report, action, live = failed_install
    expected = advance_source(root, remote)
    calls = 0
    def changed(target):
        nonlocal calls
        calls += 1
        if calls == 2: action.with_name('diagnostic.txt').write_text('changed')
        return live(target)
    result = production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
        confirmed_sync=True, automation=automation, original_execution=original, failed_material_actions=(action,),
        check_original=lambda *_: report, check_live=changed, check_configuration=lambda *_: [])
    assert result['status'] == 'blocked' and result['code'] == 'production_release_original_evidence_changed'
    assert result['source_sync_state'] == 'updated' and result['before_revision'] == before
    assert result['after_revision'] == expected and result['next_process_ready'] is False
    assert result['material_failure_source_only'] is True
    assert result['original_evidence_preserved'] == 'unknown'


@pytest.mark.parametrize('damage', ['unbound', 'duplicate', 'active_cron', 'stop', 'wrong_session', 'wrong_selection'])
def test_source_repair_preserves_original_release_gates(failed_install, damage):
    root, remote, revision, automation, original, report, action, live = failed_install
    actions = (action,)
    if damage == 'unbound': original = None
    elif damage == 'duplicate': actions = (action, action)
    elif damage == 'active_cron': automation.file.write_text(automation.file.read_text().replace('PAUSED', 'ACTIVE'))
    elif damage == 'stop': (root / 'runtime/stop-preaudit.flag').write_text('stop_requested\n')
    elif damage == 'wrong_session': report = {**report, 'tool_session_id': 'wrong'}
    elif damage == 'wrong_selection': actions = (action.with_name('wrong.json'),)
    with pytest.raises(RuntimeError):
        production_release.release_source(root, expected_revision=revision, expected_origin=str(remote),
            automation=automation, original_execution=original, failed_material_actions=actions,
            check_original=lambda *_: report, check_live=live, check_configuration=lambda *_: [])
    assert git(root, 'rev-parse', 'HEAD') == revision


def test_cli_requires_original_execution_for_material_source_only(failed_install, capsys):
    root, _, revision, automation, _, _, action, _ = failed_install
    result = production_release.main(['--target-root', str(root), '--expected-revision', revision,
        '--automation-file', str(automation.file), '--production-project-id', 'production',
        '--expected-schedule', 'preserved', '--preserve-material-failure', str(action)])
    value = json.loads(capsys.readouterr().out)
    assert result == 1 and value['code'] == 'production_release_original_execution_required'
    assert value['material_failure_source_only'] is True
    assert value['next_process_ready'] is False and value['approval_recovery_allowed'] is False
