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
