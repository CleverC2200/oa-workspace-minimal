from copy import deepcopy

import pytest

from apps.oa_finance_preaudit import production_release
from tests.finance_preaudit.test_maintenance_runtime import live_snapshot
from tests.finance_preaudit.test_production_release import git, production as production, stopped_production as stopped_production
from tests.finance_preaudit.test_request_exclusions import _closed_submission


@pytest.mark.parametrize('confirmed', [False, True])
def test_release_request_exclusion_preserves_raw_block_and_original_evidence(stopped_production, confirmed):
    root, remote, before, stop, automation, original, report = stopped_production
    stop.unlink()
    action, _, raw = _closed_submission(root)
    (root / 'decision.json').unlink()
    source = remote.parent / 'source'
    git(remote.parent, 'clone', '-q', str(remote), str(source))
    (source / 'source.txt').write_text('second')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'second')
    git(source, 'push', '-q')
    git(root, 'fetch', '-q', 'origin')
    expected = git(source, 'rev-parse', 'HEAD')
    old = {path: (path.stat().st_ino, path.read_bytes()) for path in action.parent.iterdir() if path.is_file()}
    cron = automation.file.read_bytes()
    snapshot = live_snapshot()
    snapshot['reconciliation'] = raw
    result = production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
        confirmed_sync=confirmed, automation=automation, original_execution=original,
        excluded_action_files=(action,), exclusion_issue=710,
        check_original=lambda *_: report, check_live=lambda _: deepcopy(snapshot), check_configuration=lambda *_: [])
    assert result['status'] == ('synced' if confirmed else 'ready')
    assert result['continuation'] == raw['continuation']
    assert result['continuation']['status'] == 'blocked'
    assert result['execution_continuation']['status'] == 'ready'
    assert result['next_process_ready'] is False
    assert result['scoped_next_process_ready'] is confirmed
    assert result['request_exclusion_source_only'] is True
    assert result['original_evidence_preserved'] is True
    assert result['execution_exclusions']['historical_submission_verified'] is False
    assert {path: (path.stat().st_ino, path.read_bytes()) for path in old} == old
    assert automation.file.read_bytes() == cron
    assert git(root, 'rev-parse', 'HEAD') == (expected if confirmed else before)


@pytest.mark.parametrize('damage', ['extra_conflict', 'changed_evidence'])
def test_release_request_exclusion_rejects_extra_block_or_changed_evidence(stopped_production, damage):
    root, remote, before, stop, automation, original, report = stopped_production
    stop.unlink()
    action, _, raw = _closed_submission(root)
    (root / 'decision.json').unlink()
    if damage == 'extra_conflict':
        raw['continuation']['blocking_reasons']['pending_handled_conflict'] = 1
    def read_live(_):
        if damage == 'changed_evidence':
            action.with_name('material.json').write_text('{}')
        snapshot = live_snapshot()
        snapshot['reconciliation'] = raw
        return snapshot
    result = production_release.release_source(root, expected_revision=before, expected_origin=str(remote),
        confirmed_sync=True, automation=automation, original_execution=original,
        excluded_action_files=(action,), exclusion_issue=710,
        check_original=lambda *_: report, check_live=read_live, check_configuration=lambda *_: [])
    assert result['status'] == 'blocked'
    assert result['source_sync_state'] == 'not_updated'
    assert result['next_process_ready'] is False
    assert git(root, 'rev-parse', 'HEAD') == before


@pytest.mark.parametrize('damage', [None, 'writer', 'changed_claim', 'other_material'])
def test_target_reader_excludes_only_pinned_collecting_claim_without_restoring_recovery(tmp_path, monkeypatch, damage):
    import hashlib
    import json
    from pathlib import Path
    import shutil
    import sys
    from apps.oa_finance_preaudit import maintenance_runtime, production_config, reconciliation, request_exclusions
    from tests.finance_preaudit.test_request_exclusions import _collecting_claim
    from tests.finance_preaudit.test_flow_engine_submission import _engine
    for key in ('OA_FROZEN_WRITER_LOCK_FILE', 'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE',
            'OA_FINANCE_PREAUDIT_STOP_FILE', 'OA_FINANCE_PREAUDIT_RUNTIME_ROOT'):
        monkeypatch.delenv(key, raising=False)
    action, rows, _ = _collecting_claim(tmp_path)
    recovery = action.with_name('recovery-receipt.json')
    recovery.write_text('{}')
    engine = _engine(tmp_path / 'runtime/finance_preaudit_flows', 'other-pending-request')
    other = engine.next_action('travel_expense')
    state = json.loads(Path(other['state_file']).read_text())
    rows.append({'request_id':state['request_id'], 'received_at':state['received_at'], 'workflow_key':state['workflow_key']})
    raw = reconciliation.reconcile(tmp_path / 'runtime/finance_preaudit_flows', pending_rows=rows, handled_rows=[])
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=714)
    source = Path(maintenance_runtime.__file__).resolve().parents[2]
    shutil.copytree(source / 'apps', tmp_path / 'apps', ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path / 'apps/oa_finance_preaudit/request_exclusions.py').unlink()
    interpreter = tmp_path / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('#!/bin/sh\nexec "' + sys.executable + '" "$@"\n')
    interpreter.chmod(0o700)
    auth_env = tmp_path.parent / (tmp_path.name + '-auth.env')
    auth_env.write_text('')
    auth_env.chmod(0o600)
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(auth_env))
    monkeypatch.delenv('OA_FINANCE_PREAUDIT_WRITE_POLICY', raising=False)
    policy = tmp_path / 'runtime/control/write-policy.json'
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({'schema_version':1, 'kind':'preaudit_write_policy',
        'authorized_workflows':[*production_config.FINANCE_WORKFLOWS, 'packaging_layout_approval']}))
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({'reconciliation':raw}))
    receipt = {'path':str(status), 'sha256':hashlib.sha256(status.read_bytes()).hexdigest()}
    with pytest.raises(RuntimeError, match='maintenance_frozen_inputs_untrusted'):
        maintenance_runtime.read_current_inputs(tmp_path, receipt)
    proof = {'kind':'preaudit_collecting_exclusions_source_only', 'target_root':str(tmp_path), 'items':scope.receipt()['items']}
    if damage == 'writer':
        action.with_name('writer-boundary.json').write_text('{}')
    elif damage == 'changed_claim':
        action.write_bytes(action.read_bytes() + b'\n')
    elif damage == 'other_material':
        Path(other['action_file']).with_name('material.json').write_text('{}')
    before = {file:(file.stat().st_ino, file.read_bytes()) for file in action.parent.iterdir() if file.is_file()}
    if damage:
        with pytest.raises(RuntimeError, match='maintenance_frozen_inputs_untrusted'):
            maintenance_runtime.read_current_inputs(tmp_path, receipt, source_only_proof=proof, require_reaped=True)
    else:
        result = maintenance_runtime.read_current_inputs(tmp_path, receipt, source_only_proof=proof, require_reaped=True)
        assert result['excluded_collecting_actions'] == [str(action)]
        assert result['excluded_input_validation'] == 'not_performed'
        assert result['validated_frozen_count'] == result['pending_frozen_count'] == 1
        assert result['status_sha256'] == receipt['sha256']
        assert scope.project(raw)['scope_excluded_pending'] == 1
    assert {file:(file.stat().st_ino, file.read_bytes()) for file in before} == before
    assert hashlib.sha256(status.read_bytes()).hexdigest() == receipt['sha256']


@pytest.mark.parametrize('missing_proof', [False, True])
def test_release_default_adapter_binds_collecting_exclusion_to_current_input_proof(stopped_production, monkeypatch, missing_proof):
    from apps.oa_finance_preaudit import maintenance_runtime
    from tests.finance_preaudit.test_request_exclusions import _collecting_claim
    root, remote, before, stop, automation, original, report = stopped_production
    stop.unlink()
    action, _, raw = _collecting_claim(root)
    seen = []
    def read_live(target, **kwargs):
        assert target == root
        proof = kwargs['source_only_proof']
        assert proof['kind'] == 'preaudit_collecting_exclusions_source_only'
        assert proof['target_root'] == str(root)
        assert [item['action_file'] for item in proof['items']] == [str(action)]
        seen.append(proof)
        snapshot = live_snapshot()
        snapshot['reconciliation'] = deepcopy(raw)
        if not missing_proof:
            snapshot['maintenance_current_inputs'].update(excluded_collecting_actions=[str(action)],
                excluded_input_validation='not_performed')
        return snapshot
    monkeypatch.setattr(maintenance_runtime, 'read_live_status', read_live)
    result = production_release.release_source(root, expected_revision=before, expected_origin=str(remote),
        automation=automation, original_execution=original, excluded_action_files=(action,), exclusion_issue=714,
        check_original=lambda *_: report, check_configuration=lambda *_: [])
    assert seen
    assert result['status'] == ('blocked' if missing_proof else 'ready')
    assert result['next_process_ready'] is False
    if missing_proof:
        assert result['code'] == 'maintenance_current_inputs_untrusted'
    else:
        assert result['pre_sync_current_input_proof']['excluded_input_validation'] == 'not_performed'
        assert result['execution_exclusions']['items'][0]['source_execution_state'] == 'collecting'
