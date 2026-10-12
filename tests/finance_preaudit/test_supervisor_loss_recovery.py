from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sqlite3
import os
import time

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import item_failure, maintenance_runtime, production_config, reconciliation, supervisor_loss_recovery
from scripts.finance_preaudit_run import read_execution
from tests.finance_preaudit.test_production_release import git
from tests.finance_preaudit.test_maintenance_runtime import clean_control_environment
from tests.finance_preaudit.test_supervisor_loss_maintenance import (
    current_snapshot,
    lost_execution,
)


@pytest.fixture
def recovery_target(lost_execution, monkeypatch):
    root, original, action = lost_execution
    source = Path(supervisor_loss_recovery.__file__).resolve().parents[2]
    for relative in production_config.REPOSITORY_INPUTS[:2]:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / relative).read_bytes())
    (root / '.gitignore').write_text('runtime/\n.venv/\nstatus.json\n')
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'offline recovery source')
    revision = git(root, 'rev-parse', 'HEAD')
    remote = root.parent / 'remote.git'
    git(root.parent, 'init', '--bare', '-q', '--initial-branch=main', str(remote))
    git(root, 'remote', 'add', 'origin', str(remote))
    git(root, 'push', '-q', '-u', 'origin', 'main')
    stop = root / 'runtime/stop-preaudit.flag'
    runtime_control.write_control_bytes_atomic(stop, b'stop_requested:codex_review_technical_failure\n')
    progress_file = root / ('runtime/control/approval-guard/runs/' + 'a' * 32 + '/progress.json')
    progress = json.loads(progress_file.read_bytes())
    progress['actions'] = []
    runtime_control.write_control_bytes_atomic(progress_file, json.dumps(progress).encode())
    home = root.parent / 'native-codex'
    monkeypatch.setenv('CODEX_HOME', str(home))
    cron = home / 'automations/oa-skill/automation.toml'
    cron.parent.mkdir(parents=True)
    cron.write_text('id = "oa-skill"\nstatus = "PAUSED"\nkind = "cron"\nexecution_environment = "local"\n'
        'model = "gpt-6.1-sol"\nreasoning_effort = "medium"\nrrule = "preserved"\n'
        + 'cwds = ' + json.dumps([str(root)]) + '\nprompt = '
        + json.dumps((root / 'config/automations/oa-skill-prompt.txt').read_text(), ensure_ascii=False)
        + '\n[target]\ntype = "project"\nproject_id = "production"\n')
    cron.chmod(0o600)
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads (id TEXT, cwd TEXT, model_provider TEXT)')
    with sqlite3.connect(home / 'thread_history_1.sqlite') as db:
        db.execute('CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, error_json TEXT, completed_at REAL, status TEXT)')
    args = ['--target-root', str(root), '--expected-revision', revision,
        '--expected-origin', str(remote), '--automation-file', str(cron),
        '--production-project-id', 'production', '--expected-schedule', 'preserved',
        '--original-receipt-file', str(original.file), '--original-owner-task-id', original.owner_task_id,
        '--original-tool-session-id', original.tool_session_id, '--recover-action-file', str(action)]
    return root, original, action, stop, cron, args


def test_public_recovery_preflight_keeps_unknown_wait_and_all_controls(recovery_target, capsys, monkeypatch):
    root, original, action, stop, cron, args = recovery_target
    files = [*original.file.parent.iterdir(), action, action.with_name('state.json'),
        action.with_name('manual-review-receipt.json'), stop, cron]
    before = {path: path.read_bytes() for path in files}
    code = supervisor_loss_recovery.main(args, check_live=lambda target: current_snapshot(target, original, action))
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result
    assert result['status'] == 'ready'
    assert result['stop_cleared'] is False
    assert result['original_wait_verified'] is False
    assert result['original_exit_code'] is None
    assert result['historical_stop_attribution'] == 'not_verified'
    assert result['selected_recovery_action'] == str(action)
    assert result['automation_state'] == 'PAUSED'
    assert result['write_state'] == 'not_requested'
    assert result['production_acceptance'] == 'not_verified'
    assert {path: path.read_bytes() for path in files} == before
    assert not list((root / 'runtime/control').glob('supervisor-loss-recovery-*'))
    with monkeypatch.context() as reader:
        reader.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(root / 'runtime/control/rolling-controller.lock'))
        observed = read_execution(original.file, owner_task_id=original.owner_task_id,
            invocation_id=original.file.parent.name, tool_session_id=original.tool_session_id)
    assert observed['state'] == 'supervisor_lost'
    assert observed['terminal'] is False
    assert observed['exit_code'] is None


def test_public_confirmed_recovery_only_clears_audited_stop(recovery_target, capsys, monkeypatch):
    root, original, action, stop, cron, args = recovery_target
    protected = {path:path.read_bytes() for path in (*original.file.parent.iterdir(), action,
        action.with_name('state.json'), action.with_name('manual-review-receipt.json'), cron)}
    code = supervisor_loss_recovery.main([*args, '--confirm-recovery', 'YES'],
        check_live=lambda target: current_snapshot(target, original, action))
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result
    assert result['status'] == 'cleared'
    assert result['stop_cleared'] is True
    assert result['approval_started'] is False
    assert result['continuation']['status'] == 'blocked'
    assert result['original_wait_verified'] is False
    assert result['original_exit_code'] is None
    assert result['historical_stop_attribution'] == 'not_verified'
    assert not stop.exists()
    assert {path:path.read_bytes() for path in protected} == protected
    assert not action.parent.with_name('attempt-2').exists()
    audit = Path(result['receipt_file'])
    assert audit.stat().st_mode & 0o077 == 0
    assert json.loads(audit.read_bytes()) == result
    with monkeypatch.context() as reader:
        reader.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(root / 'runtime/control/rolling-controller.lock'))
        observed = read_execution(original.file, owner_task_id=original.owner_task_id,
            invocation_id=original.file.parent.name, tool_session_id=original.tool_session_id)
    assert observed['state'] == 'supervisor_lost'
    assert observed['exit_code'] is None


@pytest.mark.parametrize('scenario', [
    'active_supervisor', 'wrong_owner', 'wrong_session', 'unreaped', 'active_cron',
    'other_stop', 'wrong_selection', 'missing_manual', 'writer_artifact', 'frozen_artifact',
    'other_failure', 'unknown_write', 'other_block', 'official_notice', 'history_unavailable',
    'unknown_slot', 'missing_policy', 'status_changed', 'cooldown',
])
def test_public_recovery_rejects_unsafe_conditions_without_clearing(recovery_target, capsys, scenario):
    root, original, action, stop, cron, args = recovery_target
    if scenario == 'active_supervisor':
        value = json.loads(original.file.read_bytes())
        value['supervisor_pid'] = os.getpid()
        runtime_control.write_control_bytes_atomic(original.file, json.dumps(value).encode())
    elif scenario == 'wrong_owner':
        args[args.index('--original-owner-task-id') + 1] = 'different-owner'
    elif scenario == 'wrong_session':
        args[args.index('--original-tool-session-id') + 1] = 'different-session'
    elif scenario == 'unreaped':
        mirror = original.file.with_name('controller-manifest.json')
        value = json.loads(mirror.read_bytes())
        value['children_reaped'] = False
        for path in (mirror, root / 'runtime/control/rolling-controller-manifest.json'):
            runtime_control.write_control_bytes_atomic(path, json.dumps(value).encode())
    elif scenario == 'active_cron':
        cron.write_text(cron.read_text().replace('PAUSED', 'ACTIVE'))
    elif scenario == 'other_stop':
        runtime_control.write_control_bytes_atomic(stop, b'stop_requested:writer_result_unknown\n')
    elif scenario == 'wrong_selection':
        args[-1] = str(action.parent.with_name('attempt-2') / 'action.json')
    elif scenario == 'missing_manual':
        action.with_name('manual-review-receipt.json').unlink()
    elif scenario in {'writer_artifact', 'frozen_artifact'}:
        runtime_control.write_control_bytes_atomic(action.with_name(
            'writer-boundary.json' if scenario == 'writer_artifact' else 'material.json'), b'{}')
    elif scenario == 'other_failure':
        for path in (action, action.with_name('state.json')):
            value = json.loads(path.read_bytes())
            value['failure_detail_code'] = 'unknown_transport_failure'
            runtime_control.write_control_bytes_atomic(path, json.dumps(value).encode())
        item_failure.persist(action.parent, value)
    elif scenario == 'official_notice':
        home = cron.parent.parent.parent
        with sqlite3.connect(home / 'state_5.sqlite') as db:
            db.execute('INSERT INTO threads VALUES (?,?,?)', ('oa', str(root), 'openai'))
        with sqlite3.connect(home / 'thread_history_1.sqlite') as db:
            db.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?)',
                ('oa', 'error', json.dumps({'codexErrorInfo':'usageLimitExceeded'}), time.time(), 'failed'))
    elif scenario == 'history_unavailable':
        (cron.parent.parent.parent / 'state_5.sqlite').unlink()
    elif scenario == 'unknown_slot':
        runtime_control.write_control_bytes_atomic(root / 'runtime/control/approval-guard/model-slot-0.json', b'{}')
    elif scenario == 'missing_policy':
        (root / 'runtime/control/write-policy.json').unlink()
    elif scenario == 'cooldown':
        stamp = time.time()
        runtime_control.write_control_bytes_atomic(root / 'runtime/control/approval-guard/cooldown.json',
            json.dumps({'schema_version':1, 'kind':'approval_model_cooldown',
                'triggered_at':stamp, 'blocked_until':stamp + 10800, 'notice_code':'usage_limit',
                'sources':['a' * 64], 'run_id':None}).encode())
    protected = {path:path.read_bytes() for path in (stop, cron, original.file, action, action.with_name('state.json'))}
    def live(target):
        snapshot = current_snapshot(target, original, action)
        if scenario == 'unknown_write':
            snapshot['reconciliation']['findings'][0]['local_write_state'] = 'unknown'
        elif scenario == 'other_block':
            snapshot['reconciliation']['continuation']['blocking_reasons']['identity_conflict'] = 1
        elif scenario == 'status_changed':
            runtime_control.write_control_bytes_atomic(Path(snapshot['maintenance_status_file']['path']), b'{}')
        return snapshot
    code = supervisor_loss_recovery.main([*args, '--confirm-recovery', 'YES'], check_live=live)
    result = json.loads(capsys.readouterr().out)
    assert code == 1, result
    assert result['status'] == 'blocked'
    assert result['stop_cleared'] is False
    assert {path:path.read_bytes() for path in protected} == protected
    assert not action.parent.with_name('attempt-2').exists()


@pytest.mark.parametrize('name', ['stop', 'cron', 'action', 'policy', 'execution'])
def test_public_recovery_rejects_same_content_replacement_after_live_read(recovery_target, capsys, name):
    root, original, action, stop, cron, args = recovery_target
    def live(target):
        snapshot = current_snapshot(target, original, action)
        path = {'stop':stop, 'cron':cron, 'action':action, 'policy':root / 'runtime/control/write-policy.json',
            'execution':original.file}[name]
        replacement = path.with_name(path.name + '.replacement')
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(path.stat().st_mode & 0o777)
        replacement.replace(path)
        return snapshot
    code = supervisor_loss_recovery.main([*args, '--confirm-recovery', 'YES'], check_live=live)
    result = json.loads(capsys.readouterr().out)
    assert code == 1, result
    assert result['stop_cleared'] is False
    assert stop.exists()


@pytest.mark.parametrize('stage', ['validated', 'cleared'])
def test_public_recovery_reports_audit_write_failure_without_claiming_success(recovery_target, capsys, monkeypatch, stage):
    root, original, action, stop, cron, args = recovery_target
    replace = os.replace
    def fail_audit(source, destination, **kwargs):
        if Path(destination).name == 'receipt.json':
            payload = json.loads(Path(source).read_bytes()) if Path(source).is_absolute() else None
            # Atomic control writes use directory-relative names.
            if payload is None:
                directory_fd = kwargs['src_dir_fd']
                descriptor = os.open(source, os.O_RDONLY, dir_fd=directory_fd)
                with os.fdopen(descriptor, 'rb') as stream:
                    payload = json.load(stream)
            if payload['status'] == stage:
                raise OSError('offline audit write failure')
        return replace(source, destination, **kwargs)
    monkeypatch.setattr(os, 'replace', fail_audit)
    code = supervisor_loss_recovery.main([*args, '--confirm-recovery', 'YES'],
        check_live=lambda target: current_snapshot(target, original, action))
    result = json.loads(capsys.readouterr().out)
    assert code == 1
    assert result['status'] == 'blocked'
    assert result['stop_cleared'] is (stage == 'cleared')
    assert stop.exists() is (stage == 'validated')
    assert 'PAUSED' in cron.read_text()


@pytest.mark.parametrize('changed', ['execution', 'policy', 'stop', 'cron'])
def test_public_recovery_rechecks_after_durable_audit_before_clear(recovery_target, capsys, monkeypatch, changed):
    root, original, action, stop, cron, args = recovery_target
    replace = os.replace
    altered = False
    def drift(source, destination, **kwargs):
        nonlocal altered
        result = replace(source, destination, **kwargs)
        if Path(destination).name == 'receipt.json' and not altered:
            altered = True
            path = {'execution':original.file, 'policy':root / 'runtime/control/write-policy.json',
                'stop':stop, 'cron':cron}[changed]
            replacement = path.with_name(path.name + '.changed')
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(path.stat().st_mode & 0o777)
            replace(replacement, path)
        return result
    monkeypatch.setattr(os, 'replace', drift)
    code = supervisor_loss_recovery.main([*args, '--confirm-recovery', 'YES'],
        check_live=lambda target: current_snapshot(target, original, action))
    result = json.loads(capsys.readouterr().out)
    assert altered
    assert code == 1
    assert result['stop_cleared'] is False
    assert stop.exists()


@pytest.mark.parametrize('changed,expected_state', [('active', 'ACTIVE'), ('damaged', 'unknown'), ('replacement', 'PAUSED')])
def test_public_recovery_observes_cron_changes_after_clear(recovery_target, capsys, monkeypatch, changed, expected_state):
    root, original, action, stop, cron, args = recovery_target
    replace = os.replace
    writes = 0
    def drift(source, destination, **kwargs):
        nonlocal writes
        result = replace(source, destination, **kwargs)
        if Path(destination).name == 'receipt.json':
            writes += 1
            if writes == 2:
                if changed == 'replacement':
                    replacement = cron.with_name('replacement.toml')
                    replacement.write_bytes(cron.read_bytes())
                    replacement.chmod(cron.stat().st_mode & 0o777)
                    replace(replacement, cron)
                else:
                    cron.write_text(cron.read_text().replace('PAUSED', 'ACTIVE') if changed == 'active' else 'invalid toml [')
        return result
    monkeypatch.setattr(os, 'replace', drift)
    code = supervisor_loss_recovery.main([*args, '--confirm-recovery', 'YES'],
        check_live=lambda target: current_snapshot(target, original, action))
    result = json.loads(capsys.readouterr().out)
    assert code == 1, result
    assert result['status'] == 'blocked'
    assert result['stop_cleared'] is True
    assert result['automation_state'] == expected_state
    assert not stop.exists()


@pytest.mark.parametrize('scenario', ['duplicate_pending', 'wrong_workflow'])
def test_public_recovery_requires_unique_live_matching_candidate(recovery_target, capsys, scenario):
    root, original, action, stop, cron, args = recovery_target
    protected = {path:path.read_bytes() for path in (stop, cron, original.file, action, action.with_name('state.json'))}
    def live(target):
        snapshot = current_snapshot(target, original, action)
        state = json.loads(action.with_name('state.json').read_bytes())
        row = {'request_id':state['request_id'], 'received_at':state['received_at'], 'workflow_key':state['workflow_key']}
        rows = [row, dict(row)] if scenario == 'duplicate_pending' else [{**row, 'workflow_key':'different_workflow'}]
        snapshot['reconciliation'] = reconciliation.reconcile(root / 'runtime/finance_preaudit_flows',
            pending_rows=rows, handled_rows=[])
        assert snapshot['reconciliation']['findings'][0]['recommendation'] != 'confirmed_no_write_recovery_candidate'
        raw = json.dumps(snapshot).encode()
        status_file = Path(snapshot['maintenance_status_file']['path'])
        runtime_control.write_control_bytes_atomic(status_file, raw)
        snapshot['maintenance_status_file']['sha256'] = hashlib.sha256(raw).hexdigest()
        report = maintenance_runtime.read_original_execution(root, original, supervisor_loss_source_only=True)
        snapshot['maintenance_current_inputs'] = maintenance_runtime.read_current_inputs(root,
            snapshot['maintenance_status_file'], source_only_proof=report['source_only_proof'])
        return snapshot
    code = supervisor_loss_recovery.main([*args, '--confirm-recovery', 'YES'], check_live=live)
    result = json.loads(capsys.readouterr().out)
    assert code == 1, result
    assert result['stop_cleared'] is False
    assert {path:path.read_bytes() for path in protected} == protected
