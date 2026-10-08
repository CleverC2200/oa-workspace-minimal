from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import production_release
from tests.finance_preaudit.test_maintenance_runtime import live_snapshot


def git(root, *args):
    return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args],
                          cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def production(tmp_path, monkeypatch):
    for key in ('OA_FROZEN_WRITER_LOCK_FILE', 'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE',
                'OA_FINANCE_PREAUDIT_STOP_FILE', 'OA_FINANCE_PREAUDIT_RUNTIME_ROOT',
                'OA_FINANCE_PREAUDIT_STOP_REQUESTED'):
        monkeypatch.delenv(key, raising=False)
    remote = tmp_path / 'remote.git'
    remote.mkdir()
    git(remote, 'init', '--bare', '-q', '--initial-branch=main')
    root = tmp_path / 'oa-workspace-minimal'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    (root / '.gitignore').write_text('runtime/\n')
    (root / 'source.txt').write_text('first')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'first')
    git(root, 'remote', 'add', 'origin', str(remote))
    git(root, 'push', '-q', '-u', 'origin', 'main')
    manifest = root / 'runtime/control/rolling-controller-manifest.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"state":"failed"}')
    return root, remote, git(root, 'rev-parse', 'HEAD')


def test_release_preflight_retains_leases_across_real_ff_and_readbacks(production):
    root, remote, before = production
    source = remote.parent / 'source'
    git(remote.parent, 'clone', '-q', str(remote), str(source))
    (source / 'source.txt').write_text('second')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'second')
    git(source, 'push', '-q')
    git(root, 'fetch', '-q', 'origin')
    expected = git(source, 'rev-parse', 'HEAD')
    observations = []
    def check_live(target):
        observations.append(git(target, 'rev-parse', 'HEAD'))
        assert runtime_control.controller_active(target / 'runtime/control/rolling-controller.lock')
        assert runtime_control.active_request_writers(target / 'runtime/control/frozen-writer.lock') == 1
        return live_snapshot()
    result = production_release.release_source(root, expected_revision=expected,
        expected_origin=str(remote), confirmed_sync=True, check_live=check_live,
        check_configuration=lambda target, revision: [])
    assert result['status'] == 'synced'
    assert result['write_state'] == 'not_requested'
    assert result['before_revision'] == before
    assert observations == [before, expected]
    assert git(root, 'rev-parse', 'HEAD') == expected


@pytest.mark.parametrize('scenario,code', [
    ('stop', 'production_release_stop_requested'),
    ('environment_stop', 'production_release_stop_requested'),
    ('dirty', 'production_release_dirty'),
    ('branch', 'production_release_main_required'),
    ('live', 'maintenance_live_identity_untrusted'),
    ('config', 'production_release_configuration_invalid'),
])
def test_release_refuses_unsafe_states_without_source_change(production, monkeypatch, scenario, code):
    root, remote, expected = production
    if scenario == 'stop':
        (root / 'runtime/stop-preaudit.flag').write_text('stop_requested\n')
    elif scenario == 'environment_stop':
        monkeypatch.setenv('OA_FINANCE_PREAUDIT_STOP_REQUESTED', 'true')
    elif scenario == 'dirty':
        (root / 'source.txt').write_text('dirty')
    elif scenario == 'branch':
        git(root, 'switch', '-qc', 'codex/wrong')
    snapshot = live_snapshot()
    if scenario == 'live':
        snapshot['live_check']['oa']['execution_identity'] = 'mismatch'
    with pytest.raises(RuntimeError, match=code):
        production_release.release_source(root, expected_revision=expected,
            expected_origin=str(remote), confirmed_sync=True, check_live=lambda target: snapshot,
            check_configuration=lambda target, revision: ['drift'] if scenario == 'config' else [])
    assert git(root, 'rev-parse', 'HEAD') == expected


def test_release_does_not_mutate_source_in_default_preflight(production):
    root, remote, expected = production
    result = production_release.release_source(root, expected_revision=expected,
        expected_origin=str(remote), check_live=lambda target: live_snapshot(),
        check_configuration=lambda target, revision: [])
    assert result['status'] == 'ready'
    assert result['source_sync_requested'] is False
    assert result['production_acceptance'] == 'not_verified'


def test_release_rechecks_stop_after_live_read_before_merge(production):
    root, remote, expected = production
    def live(target):
        (target / 'runtime/stop-preaudit.flag').write_text('stop_requested\n')
        return live_snapshot()
    with pytest.raises(RuntimeError, match='production_release_stop_requested'):
        production_release.release_source(root, expected_revision=expected,
            expected_origin=str(remote), confirmed_sync=True, check_live=live,
            check_configuration=lambda target, revision: [])
    assert (root / 'runtime/stop-preaudit.flag').read_text() == 'stop_requested\n'


def test_release_refuses_changed_terminal_manifest(production):
    root, remote, expected = production
    def live(target):
        (target / 'runtime/control/rolling-controller-manifest.json').write_text('{"state":"running"}')
        return live_snapshot()
    with pytest.raises(RuntimeError, match='production_release_manifest_changed'):
        production_release.release_source(root, expected_revision=expected,
            expected_origin=str(remote), confirmed_sync=True, check_live=live,
            check_configuration=lambda target, revision: [])


def test_release_rechecks_configuration_inside_leases_before_ff(production):
    root, remote, expected = production
    calls = []
    def config(target, revision):
        calls.append(runtime_control.controller_active(target / 'runtime/control/rolling-controller.lock'))
        return ['automation_changed'] if len(calls) == 2 else []
    with pytest.raises(RuntimeError, match='production_release_configuration_invalid'):
        production_release.release_source(root, expected_revision=expected,
            expected_origin=str(remote), confirmed_sync=True, check_live=lambda target: live_snapshot(),
            check_configuration=config)
    assert calls == [False, True]


def test_real_release_adapter_requires_actual_automation_contract(production):
    root, remote, expected = production
    with pytest.raises(RuntimeError, match='production_release_automation_required'):
        production_release.release_source(root, expected_revision=expected, expected_origin=str(remote))


def test_public_cli_returns_small_blocked_receipt_for_stop(production, capsys):
    import json
    root, remote, expected = production
    (root / 'runtime/stop-preaudit.flag').write_text('stop_requested\n')
    exit_code = production_release.main(['--target-root', str(root), '--expected-revision', expected,
        '--automation-file', str(root / 'not-opened.toml'), '--production-project-id', 'production',
        '--expected-schedule', 'preserved'])
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result == {'status': 'blocked', 'code': 'production_release_stop_requested', 'write_state': 'not_requested'}


def test_release_refuses_diverged_clean_main(production):
    root, remote, expected = production
    (root / 'source.txt').write_text('local-only')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'diverged')
    with pytest.raises(RuntimeError, match='production_release_not_fast_forward'):
        production_release.release_source(root, expected_revision=expected,
            expected_origin=str(remote), confirmed_sync=True, check_live=lambda target: live_snapshot(),
            check_configuration=lambda target, revision: [])
    assert git(root, 'rev-parse', 'HEAD') != expected


def test_release_rejects_shared_git_directory(production):
    root, remote, expected = production
    shared = root.parent / 'shared.git'
    (root / '.git').rename(shared)
    (root / '.git').symlink_to(shared, target_is_directory=True)
    with pytest.raises(RuntimeError, match='production_release_clone_required'):
        production_release.release_source(root, expected_revision=expected,
            expected_origin=str(remote), check_live=lambda target: live_snapshot(),
            check_configuration=lambda target, revision: [])
