import json
import os
from pathlib import Path
import time

import pytest

from apps.oa_finance_preaudit import owner_restart_recovery as recovery, production_release as release
from tests.finance_preaudit.test_maintenance_runtime import clean_control_environment
from tests.finance_preaudit.test_owner_restart_recovery import restart_target
from tests.finance_preaudit.test_production_release import git


@pytest.mark.parametrize('restart_target', ['user_stop'], indirect=True)
def test_user_stop_requires_explicit_resume_and_preserves_original_terminal(restart_target, capsys):
    root, original, action, cron, args, live = restart_target
    saved = json.loads(original.file.read_bytes())
    saved['owner_process_lost'] = False
    original.file.write_text(json.dumps(saved))
    before = original.file.read_bytes()
    assert recovery.main(args, check_live=live) == 1
    capsys.readouterr()
    assert recovery.main([*args, '--resume-user-stop', 'YES'], check_live=live) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['status'] == 'ready' and preview['stop_cleared'] is False
    assert (root / 'runtime/stop-preaudit.flag').exists() and original.file.read_bytes() == before
    assert recovery.main([*args, '--resume-user-stop', 'YES', '--confirm-recovery', 'YES'], check_live=live) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'cleared' and result['stop_cleared'] is True
    assert result['owner_restart_proof']['user_stop'] is True
    assert result['owner_restart_proof']['source_only'] is False
    assert result['owner_restart_proof']['owner_process_lost'] is False
    assert original.file.read_bytes() == before
    assert not (root / 'runtime/stop-preaudit.flag').exists()
    assert 'PAUSED' in cron.read_text()


@pytest.mark.parametrize('restart_target', ['user_stop'], indirect=True)
def test_active_cooldown_blocks_user_stop_resume_but_allows_source_only_proof(restart_target, capsys):
    root, original, action, cron, args, live = restart_target
    cooldown = root / 'runtime/control/approval-guard/cooldown.json'
    cooldown.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    cooldown.write_text(json.dumps({'schema_version': 1, 'kind': 'approval_model_cooldown',
        'notice_code': 'server_overloaded', 'sources': ['f' * 64], 'run_id': 'a' * 32,
        'triggered_at': now, 'blocked_until': now + 10800}))
    cooldown.chmod(0o600)
    assert recovery.main([*args, '--resume-user-stop', 'YES', '--confirm-recovery', 'YES'], check_live=live) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['stop_cleared'] is False and (root / 'runtime/stop-preaudit.flag').exists()
    proof = recovery._read_original(root, original, (root / 'runtime/control/rolling-controller-manifest.json').read_bytes(),
                                    user_stop=True, source_only=True)
    assert proof['owner_restart_proof']['source_only'] is True
    assert proof['owner_restart_proof']['user_stop'] is True


@pytest.mark.parametrize('restart_target', ['user_stop'], indirect=True)
def test_public_source_only_release_preserves_manual_stop_and_never_restores_approval(restart_target, capsys):
    root, original, action, cron, args, live = restart_target
    # Reuse the real independently supervised failed controller fixture and its Git remote.
    revision = git(root, 'rev-parse', 'HEAD')
    automation = release.ProductionAutomation(cron, 'production', 'preserved')
    before = (root / 'runtime/stop-preaudit.flag').read_bytes()
    cooldown = root / 'runtime/control/approval-guard/cooldown.json'
    now = time.time()
    cooldown.write_text(json.dumps({'schema_version': 1, 'kind': 'approval_model_cooldown',
        'notice_code': 'server_overloaded', 'sources': ['f' * 64], 'run_id': 'a' * 32,
        'triggered_at': now, 'blocked_until': now + 10800}))
    cooldown.chmod(0o600)
    preserved = {path: path.read_bytes() for path in (original.file, cron, cooldown)}
    with pytest.raises(RuntimeError, match='stop_not_repairable'):
        release.release_source(root, expected_revision=revision, expected_origin=git(root, 'remote', 'get-url', 'origin'),
            automation=automation, stopped_maintenance=True, original_execution=original, check_live=live)
    result = release.release_source(root, expected_revision=revision, expected_origin=git(root, 'remote', 'get-url', 'origin'),
        automation=automation, stopped_maintenance=True, user_stop_source_only=True, original_execution=original,
        confirmed_sync=True, check_live=live)
    assert result['status'] == 'synced'
    assert result['stop_preserved'] is True and result['automation_preserved'] is True
    assert result['next_process_ready'] is False and result['approval_recovery_allowed'] is False
    assert (root / 'runtime/stop-preaudit.flag').read_bytes() == before
    assert {path: path.read_bytes() for path in preserved} == preserved


@pytest.mark.parametrize('restart_target', ['user_stop'], indirect=True)
@pytest.mark.parametrize('target', ['original', 'attachment'])
@pytest.mark.parametrize('phase', ['before_sync', 'after_sync'])
def test_public_source_only_release_rejects_same_byte_evidence_replacement(restart_target, target, phase):
    root, original, action, cron, _args, live = restart_target
    evidence = original.file if target == 'original' else action.parent / 'attachments/evidence.bin'
    raw = evidence.read_bytes()
    inode = evidence.stat().st_ino
    stop = root / 'runtime/stop-preaudit.flag'
    preserved = {path: path.read_bytes() for path in (stop, cron)}
    calls = 0

    def replacing_live(path):
        nonlocal calls
        snapshot = live(path)
        calls += 1
        if calls == (1 if phase == 'before_sync' else 2):
            replacement = evidence.with_name('replacement-' + evidence.name)
            replacement.write_bytes(raw)
            replacement.chmod(0o600)
            os.replace(replacement, evidence)
            assert evidence.stat().st_ino != inode
        return snapshot

    options = dict(expected_revision=git(root, 'rev-parse', 'HEAD'),
        expected_origin=git(root, 'remote', 'get-url', 'origin'),
        automation=release.ProductionAutomation(cron, 'production', 'preserved'),
        stopped_maintenance=True, user_stop_source_only=True, original_execution=original,
        confirmed_sync=True, check_live=replacing_live)
    if phase == 'before_sync':
        with pytest.raises(RuntimeError, match='production_release_original_evidence_changed'):
            release.release_source(root, **options)
    else:
        result = release.release_source(root, **options)
        assert result['status'] == 'blocked'
        assert result['code'] == 'production_release_original_evidence_changed'
        assert result['user_stop_source_only'] is True
        assert result['original_evidence_preserved'] == 'unknown'
        assert result['approval_recovery_allowed'] is False
    assert {path: path.read_bytes() for path in preserved} == preserved


@pytest.mark.parametrize('restart_target', ['user_stop'], indirect=True)
@pytest.mark.parametrize('phase', ['before_sync', 'after_sync'])
def test_public_source_only_cli_failure_keeps_manual_stop_mode_and_real_revision(
    restart_target, capsys, monkeypatch, phase,
):
    root, original, _action, cron, _args, live = restart_target
    before_revision = git(root, 'rev-parse', 'HEAD')
    remote = git(root, 'remote', 'get-url', 'origin')
    source = root.parent / 'source'
    git(root.parent, 'clone', '-q', remote, str(source))
    (source / 'source-marker.txt').write_text('offline source revision\n')
    git(source, 'add', 'source-marker.txt')
    git(source, 'commit', '-qm', 'offline next source')
    git(source, 'push', '-q')
    git(root, 'fetch', '-q', 'origin')
    expected = git(source, 'rev-parse', 'HEAD')
    stop = root / 'runtime/stop-preaudit.flag'
    preserved = {path: path.read_bytes() for path in (stop, cron, original.file)}
    calls = 0

    def failing_live(target):
        nonlocal calls
        calls += 1
        if calls == (1 if phase == 'before_sync' else 2):
            raise RuntimeError('maintenance_live_status_failed')
        return live(target)

    original_release = release.release_source
    monkeypatch.setattr(release, 'release_source', lambda *args, **kwargs: original_release(
        *args, **kwargs, expected_origin=remote, check_live=failing_live))
    code = release.main(['--target-root', str(root), '--expected-revision', expected,
        '--automation-file', str(cron), '--production-project-id', 'production',
        '--expected-schedule', 'preserved', '--stopped-maintenance', 'YES',
        '--user-stop-source-only', 'YES', '--original-receipt-file', str(original.file),
        '--original-owner-task-id', original.owner_task_id,
        '--original-tool-session-id', original.tool_session_id, '--confirm-sync', 'YES'])
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 1 and result['status'] == 'blocked'
    assert result['code'] == 'maintenance_live_status_failed'
    assert result['user_stop_source_only'] is True
    assert result['approval_recovery_allowed'] is False
    assert result['original_wait_verified'] == 'unknown'
    assert result['original_evidence_preserved'] == 'unknown'
    assert result['next_process_ready'] is False
    assert result['before_revision'] == before_revision
    assert result['after_revision'] == (before_revision if phase == 'before_sync' else expected)
    assert result['source_sync_state'] == ('not_updated' if phase == 'before_sync' else 'updated')
    assert {path: path.read_bytes() for path in preserved} == preserved
