from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import maintenance_runtime, production_release
from tests.finance_preaudit.test_maintenance_runtime import live_snapshot


def git(root, *args):
    return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args],
                          cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _control_metadata(info):
    # Reading can update atime; inode, content timestamps and permissions must not change.
    return tuple(getattr(info, key) for key in (
        'st_dev', 'st_ino', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_uid', 'st_gid', 'st_nlink', 'st_size'))


def test_control_metadata_allows_access_time_but_detects_content_timestamp_change(tmp_path):
    before = tmp_path.stat()
    fields = {key: getattr(before, key) for key in dir(before) if key.startswith('st_')}
    fields.update(st_atime=before.st_atime + 1, st_atime_ns=before.st_atime_ns + 1_000_000_000)
    assert _control_metadata(SimpleNamespace(**fields)) == _control_metadata(before)
    fields['st_mtime_ns'] += 1
    assert _control_metadata(SimpleNamespace(**fields)) != _control_metadata(before)


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


@pytest.fixture
def stopped_production(production, monkeypatch):
    root, remote, revision = production
    stop = root / 'runtime/stop-preaudit.flag'
    runtime_control.write_control_bytes_atomic(stop, b'stop_requested:codex_review_technical_failure\n')
    native_home = root.parent / 'native-codex'
    monkeypatch.setenv('CODEX_HOME', str(native_home))
    automation_file = native_home / 'automations/oa-skill/automation.toml'
    automation_file.parent.mkdir(parents=True)
    prompt = (Path(production_release.__file__).resolve().parents[2] / 'config/automations/oa-skill-prompt.txt').read_text()
    automation_file.write_text('id = "oa-skill"\nstatus = "PAUSED"\nkind = "cron"\nexecution_environment = "local"\n'
        'model = "gpt-6.1-sol"\nreasoning_effort = "medium"\nrrule = "preserved"\n'
        + 'cwds = ' + json.dumps([str(root)]) + '\nprompt = ' + json.dumps(prompt, ensure_ascii=False)
        + '\n[target]\ntype = "project"\nproject_id = "production"\n')
    automation_file.chmod(0o600)
    automation = production_release.ProductionAutomation(automation_file, 'production', 'preserved')
    manifest = {'kind': 'preaudit_controller_run_manifest', 'state': 'failed', 'children_reaped': True,
        'run_id': 'a' * 32, 'controller_instance_id': 'b' * 32, 'controller_pid': 99999999,
        'code_revision': revision}
    runtime_control.write_control_bytes_atomic(root / 'runtime/control/rolling-controller-manifest.json',
        json.dumps(manifest).encode())
    original = maintenance_runtime.OriginalExecution(root / ('runtime/control/executions/' + 'c' * 32 + '/execution.json'),
        '00000000-0000-0000-0000-000000000001', 'original-session')
    report = {'kind': 'preaudit_controller_execution_readback', 'read_only': True, 'state': 'exited',
        'terminal': True, 'children_reaped': True, 'controller_state': 'failed', 'exit_code': 1,
        'run_id': manifest['run_id'], 'controller_instance_id': manifest['controller_instance_id'],
        'code_revision': revision, 'receipt_file': str(original.file), 'owner_task_id': original.owner_task_id,
        'invocation_id': original.file.parent.name, 'tool_session_id': original.tool_session_id}
    return root, remote, revision, stop, automation, original, report


def test_stopped_source_sync_preserves_stop_and_paused_cron_without_business_readiness(stopped_production):
    root, remote, before, stop, automation, original, report = stopped_production
    source = remote.parent / 'source'
    git(remote.parent, 'clone', '-q', str(remote), str(source))
    (source / 'source.txt').write_text('second')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'second')
    git(source, 'push', '-q')
    git(root, 'fetch', '-q', 'origin')
    expected = git(source, 'rev-parse', 'HEAD')
    stop_before = _control_metadata(stop.lstat()), stop.read_bytes()
    cron_before = automation.file.read_bytes()
    def read_original(target, binding):
        assert runtime_control.controller_active(target / 'runtime/control/rolling-controller.lock')
        assert runtime_control.active_request_writers(target / 'runtime/control/frozen-writer.lock') == 1
        assert binding == original
        return report
    result = production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
        confirmed_sync=True, stopped_maintenance=True, automation=automation, original_execution=original,
        check_original=read_original, check_live=lambda _: live_snapshot(), check_configuration=lambda *_: [])
    assert result['status'] == 'synced'
    assert result['before_revision'] == before
    assert result['after_revision'] == expected
    assert result['next_process_ready'] is False
    assert result['write_state'] == 'not_requested'
    assert result['production_acceptance'] == 'not_verified'
    assert result['stop_preserved'] is True
    assert result['automation_state'] == 'PAUSED'
    assert (_control_metadata(stop.lstat()), stop.read_bytes()) == stop_before
    assert automation.file.read_bytes() == cron_before


def test_stopped_postcheck_preserves_actual_update_and_changed_control_proof(stopped_production):
    root, remote, before, stop, automation, original, report = stopped_production
    calls = 0
    def live(_):
        nonlocal calls
        calls += 1
        if calls == 2:
            runtime_control.write_control_bytes_atomic(stop, stop.read_bytes())
        return live_snapshot()
    result = production_release.release_source(root, expected_revision=before, expected_origin=str(remote),
        confirmed_sync=True, stopped_maintenance=True, automation=automation, original_execution=original,
        check_original=lambda *_: report, check_live=live, check_configuration=lambda *_: [])
    assert result['status'] == 'blocked'
    assert result['code'] == 'production_release_stopped_controls_changed'
    assert result['stop_preserved'] is False
    assert result['stopped_controls_before']['stop'] != result['stopped_controls_after']['stop']
    assert result['stopped_controls_after']['automation_state'] == 'PAUSED'
    assert result['source_sync_state'] == 'not_updated'
    assert result['next_process_ready'] is False
    assert result['write_state'] == 'not_requested'


@pytest.mark.parametrize('scenario,code', [
    ('ordinary', 'production_release_stop_requested'),
    ('manual_stop', 'production_release_stop_not_repairable'),
    ('unknown_stop', 'production_release_stop_not_repairable'),
    ('writer_stop', 'production_release_stop_not_repairable'),
    ('cooldown_stop', 'production_release_stop_not_repairable'),
    ('unsafe_stop', 'production_release_control_untrusted'),
    ('linked_stop', 'production_release_control_untrusted'),
    ('active_cron', 'production_release_automation_not_paused'),
    ('wrong_cron', 'production_release_configuration_invalid'),
    ('original_waiting', 'maintenance_original_execution_untrusted'),
    ('original_unreaped', 'maintenance_original_execution_untrusted'),
    ('original_session', 'maintenance_original_execution_untrusted'),
    ('original_run', 'maintenance_original_execution_untrusted'),
    ('original_exit', 'maintenance_original_execution_untrusted'),
    ('original_alive', 'maintenance_original_process_active'),
    ('identity', 'maintenance_live_identity_untrusted'),
    ('authorization', 'maintenance_current_inputs_untrusted'),
    ('freeze', 'maintenance_current_inputs_untrusted'),
    ('unknown_write', 'maintenance_current_write_unresolved'),
    ('continuation', 'maintenance_reconciliation_untrusted'),
    ('unreaped_controller', 'maintenance_handoff_untrusted'),
])
def test_stopped_mode_keeps_existing_and_original_evidence_gates(stopped_production, scenario, code):
    root, remote, expected, stop, automation, original, report = stopped_production
    snapshot = live_snapshot()
    if scenario.endswith('_stop') and scenario not in {'unsafe_stop', 'linked_stop'}:
        reason = {'manual_stop': b'stop_requested\n', 'unknown_stop': b'stop_requested:unknown\n',
                  'writer_stop': b'stop_requested:writer_result_unknown\n',
                  'cooldown_stop': b'stop_requested:codex_official_error_cooldown\n'}[scenario]
        runtime_control.write_control_bytes_atomic(stop, reason)
    elif scenario == 'unsafe_stop':
        stop.chmod(0o666)
    elif scenario == 'linked_stop':
        external = root.parent / 'stop'
        stop.rename(external)
        stop.symlink_to(external)
    elif scenario == 'active_cron':
        automation.file.write_text(automation.file.read_text().replace('"PAUSED"', '"ACTIVE"'))
    elif scenario == 'wrong_cron':
        automation.file.write_text(automation.file.read_text().replace('project_id = "production"', 'project_id = "other"'))
    elif scenario.startswith('original_'):
        if scenario == 'original_alive':
            manifest_file = root / 'runtime/control/rolling-controller-manifest.json'
            manifest = json.loads(manifest_file.read_bytes())
            manifest['controller_pid'] = os.getpgrp()
            runtime_control.write_control_bytes_atomic(manifest_file, json.dumps(manifest).encode())
        else:
            key, value = {'original_waiting': ('state', 'waiting'), 'original_unreaped': ('children_reaped', False),
                'original_session': ('tool_session_id', 'new-session'), 'original_run': ('run_id', 'd' * 32),
                'original_exit': ('exit_code', 0)}[scenario]
            report[key] = value
    elif scenario == 'identity':
        snapshot['live_check']['oa']['execution_identity'] = 'mismatch'
    elif scenario == 'authorization':
        snapshot['maintenance_current_inputs']['authorized_workflows'] = []
    elif scenario == 'freeze':
        snapshot['maintenance_current_inputs']['validated_frozen_count'] = 1
    elif scenario == 'unknown_write':
        snapshot['reconciliation']['findings'] = [{'category': 'pending_not_handled', 'local_write_state': 'unknown'}]
    elif scenario == 'continuation':
        snapshot['reconciliation']['continuation']['status'] = 'blocked'
    elif scenario == 'unreaped_controller':
        snapshot['controller_handoff']['children_reaped'] = False
    before = _control_metadata(stop.lstat()), stop.read_bytes(), automation.file.read_bytes()
    with pytest.raises(RuntimeError, match=code):
        production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
            confirmed_sync=True, stopped_maintenance=scenario != 'ordinary', automation=automation,
            original_execution=original, check_original=lambda *_: report,
            check_live=lambda _: snapshot, check_configuration=lambda *_: [])
    assert git(root, 'rev-parse', 'HEAD') == expected
    assert (_control_metadata(stop.lstat()), stop.read_bytes(), automation.file.read_bytes()) == before


@pytest.mark.parametrize('when', ['original', 'live', 'configuration'])
@pytest.mark.parametrize('control', ['stop', 'cron'])
def test_stopped_sync_rejects_control_races_before_merge(stopped_production, when, control):
    root, remote, expected, stop, automation, original, report = stopped_production
    def mutate():
        path = stop if control == 'stop' else automation.file
        runtime_control.write_control_bytes_atomic(path, path.read_bytes())
    def read_original(*_):
        if when == 'original':
            mutate()
        return report
    def live(_):
        if when == 'live':
            mutate()
        return live_snapshot()
    config_calls = 0
    def config(*_):
        nonlocal config_calls
        config_calls += 1
        if when == 'configuration' and config_calls == 2:
            mutate()
        return []
    with pytest.raises(RuntimeError, match='production_release_stopped_controls_changed'):
        production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
            confirmed_sync=True, stopped_maintenance=True, automation=automation, original_execution=original,
            check_original=read_original, check_live=live, check_configuration=config)
    assert git(root, 'rev-parse', 'HEAD') == expected


def test_stopped_post_sync_failure_reports_source_updated_and_preserved_controls(stopped_production):
    root, remote, before, stop, automation, original, report = stopped_production
    source = remote.parent / 'source'
    git(remote.parent, 'clone', '-q', str(remote), str(source))
    (source / 'source.txt').write_text('second')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'second')
    git(source, 'push', '-q')
    git(root, 'fetch', '-q', 'origin')
    expected = git(source, 'rev-parse', 'HEAD')
    calls = 0
    def live(_):
        nonlocal calls
        calls += 1
        snapshot = live_snapshot()
        if calls == 2:
            snapshot['live_check']['oa']['execution_identity'] = 'mismatch'
        return snapshot
    result = production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
        confirmed_sync=True, stopped_maintenance=True, automation=automation, original_execution=original,
        check_original=lambda *_: report, check_live=live, check_configuration=lambda *_: [])
    assert result['status'] == 'blocked'
    assert result['code'] == 'maintenance_live_identity_untrusted'
    assert result['source_sync_state'] == 'updated'
    assert result['before_revision'] == before
    assert result['after_revision'] == expected
    assert result['stop_preserved'] is result['automation_preserved'] is True
    assert result['stopped_controls_before'] == result['stopped_controls_after']
    assert result['next_process_ready'] is False
    assert git(root, 'rev-parse', 'HEAD') == expected


def test_stopped_preflight_is_readonly_and_requires_original_binding(stopped_production, capsys):
    root, remote, expected, stop, automation, original, report = stopped_production
    result = production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
        stopped_maintenance=True, automation=automation, original_execution=original,
        check_original=lambda *_: report, check_live=lambda _: live_snapshot(), check_configuration=lambda *_: [])
    assert result['status'] == 'ready'
    assert result['next_process_ready'] is False
    assert result['source_sync_requested'] is False
    assert result['stop_preserved'] is True
    assert production_release.main(['--target-root', str(root), '--expected-revision', expected,
        '--automation-file', str(automation.file), '--production-project-id', 'production',
        '--expected-schedule', 'preserved', '--stopped-maintenance', 'YES']) == 1
    blocked = json.loads(capsys.readouterr().out)
    assert blocked['code'] == 'production_release_original_execution_required'
    assert blocked['stopped_controls_before'] == blocked['stopped_controls_after']
    assert blocked['stop_preserved'] is True
    assert blocked['automation_state'] == 'PAUSED'


def test_stopped_mode_accepts_native_readable_cron_without_changing_permissions(stopped_production):
    root, remote, expected, stop, automation, original, report = stopped_production
    automation.file.chmod(0o644)
    before = automation.file.stat().st_mode
    result = production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
        stopped_maintenance=True, automation=automation, original_execution=original,
        check_original=lambda *_: report, check_live=lambda _: live_snapshot(), check_configuration=lambda *_: [])
    assert result['status'] == 'ready'
    assert result['automation_preserved'] is True
    assert automation.file.stat().st_mode == before
    assert stop.stat().st_mode & 0o777 == 0o600


def test_stopped_cli_never_claims_unsafe_marker_preserved(stopped_production, capsys):
    root, remote, expected, stop, automation, original, report = stopped_production
    stop.chmod(0o666)
    assert production_release.main(['--target-root', str(root), '--expected-revision', expected,
        '--automation-file', str(automation.file), '--production-project-id', 'production',
        '--expected-schedule', 'preserved', '--stopped-maintenance', 'YES',
        '--original-receipt-file', str(original.file), '--original-owner-task-id', original.owner_task_id,
        '--original-tool-session-id', original.tool_session_id]) == 1
    blocked = json.loads(capsys.readouterr().out)
    assert blocked['stopped_controls_before']['stop'] == {'status': 'untrusted'}
    assert blocked['stopped_controls_after']['stop'] == {'status': 'untrusted'}
    assert blocked['stop_preserved'] is False
    assert blocked['next_process_ready'] is False


@pytest.mark.parametrize('substitution', ['copy', 'wrong_id', 'symlink', 'linked_registry'])
def test_stopped_mode_requires_registered_native_approval_cron(stopped_production, substitution):
    root, remote, expected, stop, automation, original, report = stopped_production
    registered = automation.file
    if substitution == 'copy':
        copied = root.parent / 'paused-copy.toml'
        copied.write_bytes(registered.read_bytes())
        copied.chmod(0o644)
        registered.write_text(registered.read_text().replace('"PAUSED"', '"ACTIVE"'))
        automation = production_release.ProductionAutomation(copied, automation.project_id, automation.schedule)
        code = 'production_release_automation_identity_mismatch'
    elif substitution == 'wrong_id':
        registered.write_text(registered.read_text().replace('id = "oa-skill"', 'id = "another-cron"'))
        code = 'production_release_automation_identity_mismatch'
    elif substitution == 'symlink':
        copied = root.parent / 'paused-copy.toml'
        registered.rename(copied)
        registered.symlink_to(copied)
        code = 'production_release_control_untrusted'
    else:
        moved = root.parent / 'moved-registry'
        registered.parent.rename(moved)
        registered.parent.symlink_to(moved, target_is_directory=True)
        code = 'control_file_unsafe'
    stop_before, cron_before = stop.read_bytes(), registered.read_bytes()
    with pytest.raises(RuntimeError, match=code):
        production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
            stopped_maintenance=True, automation=automation, original_execution=original,
            check_original=lambda *_: report, check_live=lambda _: live_snapshot(), check_configuration=lambda *_: [])
    assert git(root, 'rev-parse', 'HEAD') == expected
    assert stop.read_bytes() == stop_before
    assert registered.read_bytes() == cron_before


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


def test_release_reports_actual_source_after_post_sync_live_failure(production, monkeypatch, capsys):
    root, remote, before = production
    source = remote.parent / 'source'
    git(remote.parent, 'clone', '-q', str(remote), str(source))
    (source / 'source.txt').write_text('second')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'second')
    git(source, 'push', '-q')
    git(root, 'fetch', '-q', 'origin')
    expected = git(source, 'rev-parse', 'HEAD')
    calls = 0
    def live(target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('maintenance_live_status_failed')
        return live_snapshot()
    result = production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
        confirmed_sync=True, check_live=live, check_configuration=lambda *_: [])
    assert result['status'] == 'blocked'
    assert result['source_sync_state'] == 'updated'
    assert result['before_revision'] == before
    assert result['after_revision'] == result['requested_revision'] == expected
    assert result['code'] == 'maintenance_live_status_failed'
    assert result['next_process_ready'] is False
    assert result['production_acceptance'] == 'not_verified'
    assert git(root, 'rev-parse', 'HEAD') == expected
    monkeypatch.setattr(production_release, 'release_source', lambda *_args, **_kwargs: result)
    assert production_release.main(['--target-root', str(root), '--expected-revision', expected, '--confirm-sync', 'YES',
        '--automation-file', str(root / 'not-opened.toml'), '--production-project-id', 'production',
        '--expected-schedule', 'preserved']) == 1
    import json
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize('unknown', [False, True])
def test_post_sync_failure_reports_unchanged_or_unreadable_head(production, unknown):
    root, remote, expected = production
    calls = 0
    def live(target):
        nonlocal calls
        calls += 1
        if calls == 2:
            if unknown:
                (target / '.git').rename(target / 'moved-git')
            raise RuntimeError('maintenance_live_status_failed')
        return live_snapshot()
    result = production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
        confirmed_sync=True, check_live=live, check_configuration=lambda *_: [])
    assert result['status'] == 'blocked'
    assert result['source_sync_state'] == ('unknown' if unknown else 'not_updated')
    assert result['before_revision'] == expected
    assert result['after_revision'] == (None if unknown else expected)
    assert result['next_process_ready'] is False


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


def test_default_provider_migration_requires_updating_paused_cron_before_release(production):
    from apps.oa_finance_preaudit import production_config
    root, remote, _ = production
    source = Path(production_release.__file__).resolve().parents[2]
    for name in production_config.REPOSITORY_INPUTS:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / name).read_bytes())
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'default Codex candidate')
    git(root, 'push', '-q', 'origin', 'main')
    revision = git(root, 'rev-parse', 'HEAD')
    assert production_release.candidate_configuration_findings(root, revision) == []
    prompt = (source / 'config/automations/oa-skill-prompt.txt').read_text()
    old_prompt = prompt.replace('--review-provider openai --', '--review-provider deepseek --')
    old_prompt = old_prompt.replace('model=gpt-6.1-sol reasoning_effort=medium model_provider=openai',
                                    'model=deepseek-flash reasoning_effort=high model_provider=oa_deepseek')
    cron = root.parent / 'paused-automation.toml'
    def write_cron(text):
        cron.write_text('id = "oa-skill"\nstatus = "PAUSED"\nkind = "cron"\nexecution_environment = "local"\n'
            'model = "gpt-6.1-sol"\nreasoning_effort = "medium"\nrrule = "preserved"\n'
            + 'cwds = ' + json.dumps([str(root)]) + '\nprompt = ' + json.dumps(text, ensure_ascii=False)
            + '\n[target]\ntype = "project"\nproject_id = "production"\n')
    write_cron(old_prompt)
    before = cron.read_bytes()
    automation = production_release.ProductionAutomation(cron, 'production', 'preserved')
    with pytest.raises(RuntimeError, match='production_config_provider_selection_drift:automation'):
        production_release.release_source(root, expected_revision=revision, expected_origin=str(remote),
            automation=automation, check_live=lambda _: pytest.fail('old cron reached live release checks'))
    assert cron.read_bytes() == before and git(root, 'rev-parse', 'HEAD') == revision
    write_cron(prompt)
    result = production_release.release_source(root, expected_revision=revision, expected_origin=str(remote),
        automation=automation, check_live=lambda _: live_snapshot())
    assert result['status'] == 'ready' and result['source_sync_requested'] is False
    assert 'status = "PAUSED"' in cron.read_text()
    assert result['production_acceptance'] == 'not_verified'


def test_public_cli_returns_small_blocked_receipt_for_stop(production, capsys):
    import json
    root, remote, expected = production
    (root / 'runtime/stop-preaudit.flag').write_text('stop_requested\n')
    exit_code = production_release.main(['--target-root', str(root), '--expected-revision', expected,
        '--automation-file', str(root / 'not-opened.toml'), '--production-project-id', 'production',
        '--expected-schedule', 'preserved'])
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result['status'] == 'blocked'
    assert result['code'] == 'production_release_stop_requested'
    assert result['write_state'] == 'not_requested'
    assert result['before_revision'] == result['after_revision'] == expected
    assert result['source_sync_state'] == 'not_updated'
    assert result['production_acceptance'] == 'not_verified'


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
