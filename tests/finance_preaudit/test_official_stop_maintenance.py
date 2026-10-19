from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import maintenance_runtime, production_config, production_release
from scripts.finance_preaudit_run import bind_tool_session
from tests.finance_preaudit.test_approval_guard import prepared_case
from tests.finance_preaudit.test_flow_engine_submission import _decision, _engine, _grant, _success, _verify_grant
from tests.finance_preaudit.test_maintenance_runtime import clean_control_environment, compiled_maintenance_source, live_snapshot
from tests.finance_preaudit.test_production_release import git
from tests.finance_preaudit.test_reconciliation import _state


@pytest.fixture
def official_production(tmp_path, monkeypatch, compiled_maintenance_source, request):
    root = tmp_path / 'oa-workspace-minimal'
    root.mkdir()
    source = Path(maintenance_runtime.__file__).resolve().parents[2]
    for name in ('apps', 'scripts'):
        shutil.copytree(compiled_maintenance_source / name, root / name)
    for name in (*production_config.REPOSITORY_INPUTS[:2], production_config.MODEL_CATALOG):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    interpreter = root / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('#!/bin/sh\nexec "' + sys.executable + '" "$@"\n')
    interpreter.chmod(0o700)
    auth = tmp_path / 'offline.env'
    auth.write_text('')
    auth.chmod(0o600)
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(auth))
    _flows, _rows, _collections, _prepared_engine, action = prepared_case(root)
    _state(_flows, 'ordinary-failure', '2026-10-10 09:01:00', reason='codex_review_failed')
    failure = next(path for path in _flows.glob('*/cases/*/attempt-1/action.json') if path != action)
    completed_engine = _engine(_flows, 'completed-offline', authorization_source=_grant,
        authorization_verifier=_verify_grant, submit_frozen=_success)
    completed = completed_engine.next_action('travel_expense')
    completed_action = Path(completed['action_file'])
    decision = completed_action.with_name('review-decision.json')
    runtime_control.write_control_bytes_atomic(decision, json.dumps(_decision(completed)).encode())
    assert completed_engine.complete_review(completed_action, decision)['write_state'] == 'submitted'
    child = '''import os,json,datetime,sys
from pathlib import Path
os.chdir(sys.argv[2])
from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import approval_guard as guard, approval_lifecycle as lifecycle
root = Path.cwd()
manifest = {'kind':'preaudit_controller_run_manifest','controller_pid':os.getpid(),
 'schema_version':1,'run_id':'a'*32,'controller_instance_id':'b'*32,'state':'failed',
 'children_reaped':True,'code_revision':'c'*40,
 'code_dirty':False,'config_fingerprint':'sha256:'+'f'*64,'previous_run_id':None,
 'previous_controller_instance_id':None,'restart_mode':'fresh_process',
 'controller_lock_verified':True,'writer_inactive_at_start':True,
 'restart_verified':True,'health':'degraded',
 'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
with runtime_control.global_controller_lease(), lifecycle.run_scope(manifest, root):
 lifecycle.heartbeat(manifest['run_id'],manifest['controller_instance_id'],{Path(p) for p in json.loads(sys.argv[1])},[],writer_active=False)
 guard.record_notice('server_overloaded','offline-first-error')
 lifecycle.interrupt_unwritten(manifest,cause=guard.STOP_REASON)
manifest['completed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
for path in (Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE']),root/'runtime/control/rolling-controller-manifest.json'):
 guard.write(path,manifest)
print('offline fake only')
raise SystemExit(1)'''
    directory = root / 'runtime/control/executions' / uuid4().hex
    owner = str(uuid4())
    with monkeypatch.context() as environment:
        provider = getattr(request, 'param', 'deepseek')
        environment.setenv('OA_REVIEW_PROVIDER', provider)
        environment.setenv('OA_REVIEW_TRANSPORT', 'host_chat' if provider == 'openai' else 'cli')
        environment.chdir(root)
        environment.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE',
            str(root / 'runtime/control/rolling-controller.lock'))
        supervisor = '''import json,sys
from pathlib import Path
from scripts.finance_preaudit_run import supervise
result=supervise([sys.executable,'-B','-c',sys.argv[1],sys.argv[2],sys.argv[3]],
 directory=Path(sys.argv[4]),owner_task_id=sys.argv[5])
print(json.dumps(result))'''
        finished = subprocess.run([sys.executable, '-B', '-c', supervisor, child, json.dumps([str(p) for p in (action, failure, completed_action)]),
            str(root), str(directory), owner], cwd=root, check=True, capture_output=True, text=True)
        receipt = json.loads(finished.stdout.strip().splitlines()[-1])
        original = maintenance_runtime.OriginalExecution(Path(receipt['receipt_file']), owner, 'original-session')
        bind_tool_session(original.file, owner_task_id=owner, session_id=original.tool_session_id)
    assert receipt['state'] == 'exited' and receipt['exit_code'] == 1 and receipt['children_reaped'] is True
    runtime_control.write_control_bytes_atomic(root / 'runtime/control/write-policy.json', json.dumps({
        'schema_version': 1, 'kind': 'preaudit_write_policy',
        'authorized_workflows': [*production_config.FINANCE_WORKFLOWS, 'packaging_layout_approval'],
    }).encode())
    # Preserve the installed pre-#452 count bug in the target, not in candidate code.
    old = root / 'apps/oa_finance_preaudit/reconciliation.py'
    old.write_text(old.read_text().replace('        "codex_review_interrupted",\n', ''))
    (root / '.gitignore').write_text('runtime/\n.venv/\nstatus.json\n__pycache__/\n')
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'old installed source')
    before = git(root, 'rev-parse', 'HEAD')
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'init', '--bare', '-q', '--initial-branch=main', str(remote))
    git(root, 'remote', 'add', 'origin', str(remote))
    git(root, 'push', '-q', '-u', 'origin', 'main')
    candidate = tmp_path / 'candidate'
    git(tmp_path, 'clone', '-q', str(remote), str(candidate))
    shutil.copyfile(source / 'apps/oa_finance_preaudit/reconciliation.py', candidate / 'apps/oa_finance_preaudit/reconciliation.py')
    git(candidate, 'add', '.')
    git(candidate, 'commit', '-qm', 'correct interrupted counts')
    git(candidate, 'push', '-q')
    git(root, 'fetch', '-q', 'origin')
    expected = git(candidate, 'rev-parse', 'HEAD')
    native = tmp_path / 'native-codex'
    monkeypatch.setenv('CODEX_HOME', str(native))
    cron = native / 'automations/oa-skill/automation.toml'
    cron.parent.mkdir(parents=True)
    cron.write_text('id="oa-skill"\nstatus="PAUSED"\nkind="cron"\nexecution_environment="local"\n'
        'model="gpt-6.1-sol"\nreasoning_effort="medium"\nrrule="preserved"\ncwds=' + json.dumps([str(root)])
        + '\nprompt=' + json.dumps((source / production_config.REPOSITORY_INPUTS[0]).read_text(), ensure_ascii=False)
        + '\n[target]\ntype="project"\nproject_id="production"\n')
    cron.chmod(0o600)
    return root, original, action, before, expected, remote, cron


def offline_live(root, original):
    assert runtime_control.controller_active(root / 'runtime/control/rolling-controller.lock')
    assert runtime_control.active_request_writers(root / 'runtime/control/frozen-writer.lock') == 1
    report = maintenance_runtime.read_original_execution(root, original, official_stop_source_only=True)
    script = '''import json,sys
from pathlib import Path
from apps.oa_finance_preaudit import reconciliation
root=Path.cwd()/'runtime/finance_preaudit_flows'
states=reconciliation.latest_states(root)
row=lambda s:{k:s[k] for k in ('request_id','received_at','workflow_key')}
print(json.dumps(reconciliation.reconcile(root,pending_rows=[row(s) for s in states if s['execution_state']!='closed'],
 handled_rows=[row(s) for s in states if s['execution_state']=='closed'])))'''
    read = subprocess.run([str(root / '.venv/bin/python'), '-B', '-E', '-c', script],
        cwd=root, check=True, capture_output=True, text=True)
    snapshot = live_snapshot()
    snapshot['reconciliation'] = json.loads(read.stdout)
    raw = json.dumps(snapshot).encode()
    status = root / 'status.json'
    runtime_control.write_control_bytes_atomic(status, raw)
    snapshot['maintenance_status_file'] = {'path': str(status), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    snapshot['maintenance_current_inputs'] = maintenance_runtime.read_current_inputs(root,
        snapshot['maintenance_status_file'], official_stop_proof=report['official_stop_proof'], require_reaped=True)
    return snapshot


def run_release(fixture, monkeypatch, capsys, *, confirm=True, options=(), live=None, mode=True, stopped=True):
    root, original, _action, _before, expected, remote, cron = fixture
    release = production_release.release_source
    monkeypatch.setattr(production_release, 'release_source', lambda *args, **kwargs: release(
        *args, **kwargs, expected_origin=str(remote), check_live=live or (lambda target: offline_live(target, original))))
    argv = ['--target-root', str(root), '--expected-revision', expected, '--automation-file', str(cron),
        '--production-project-id', 'production', '--expected-schedule', 'preserved',
        *(['--stopped-maintenance', 'YES'] if stopped else []),
        *(['--official-stop-source-only', 'YES'] if mode else []), '--original-receipt-file', str(original.file),
        '--original-owner-task-id', original.owner_task_id, '--original-tool-session-id', original.tool_session_id,
        *(['--confirm-sync', 'YES'] if confirm else []), *options]
    code = production_release.main(argv)
    output = capsys.readouterr().out.strip().splitlines()[-1]
    return code, json.loads(output)


def test_official_source_only_cli_sync_preserves_cooldown_receipts_and_paused_cron(official_production, monkeypatch, capsys):
    root, original, action, before, expected, _remote, cron = official_production
    files = [*original.file.parent.iterdir(), *action.parent.glob('*.json'), cron,
        root / 'runtime/stop-preaudit.flag', root / 'runtime/control/approval-guard/cooldown.json']
    unchanged = {p: (p.stat().st_ino, p.read_bytes()) for p in files if p.is_file()}
    code, result = run_release(official_production, monkeypatch, capsys)
    assert code == 0 and result['status'] == 'synced'
    assert result['before_revision'] == before and result['after_revision'] == expected
    assert result['official_stop_source_only'] is True
    assert result['original_wait_verified'] is True and result['original_evidence_preserved'] is True
    assert result['stop_preserved'] is True and result['cooldown_preserved'] is True
    assert result['automation_state'] == 'PAUSED' and result['automation_preserved'] is True
    assert result['next_process_ready'] is False and result['approval_recovery_allowed'] is False
    assert result['write_state'] == 'not_requested'
    assert result['continuation'] == {'status': 'ready', 'scope': 'item', 'isolated_pending': 2, 'blocking_reasons': {}}
    assert {p: (p.stat().st_ino, p.read_bytes()) for p in unchanged} == unchanged


def test_official_original_readback_requires_real_wait_and_preserves_same_run_evidence(official_production):
    root, original, action, _before, _expected, _remote, _cron = official_production
    saved = original.file.read_bytes()
    report = maintenance_runtime.read_original_execution(root, original, official_stop_source_only=True)
    assert report['terminal'] is True and report['exit_code'] == 1
    assert report['official_stop_proof']['interrupted_actions'] == [str(action)]
    assert report['official_stop_proof']['readonly_reaped'] is True
    assert original.file.read_bytes() == saved


def test_official_source_only_default_preflight_keeps_old_counts_and_source(official_production, monkeypatch, capsys):
    root, _original, _action, before, _expected, _remote, _cron = official_production
    code, result = run_release(official_production, monkeypatch, capsys, confirm=False)
    assert code == 0 and result['status'] == 'ready'
    assert result['revision'] == git(root, 'rev-parse', 'HEAD') == before
    assert result['current_input_proof']['verified_interrupted_actions'] == result['current_input_proof']['uncounted_interrupted_actions']
    assert len(result['current_input_proof']['uncounted_interrupted_actions']) == 1
    original_status = json.loads(Path(result['status_file']['path']).read_bytes())
    assert original_status['reconciliation']['continuation']['isolated_pending'] == 2
    assert original_status['reconciliation']['quarantine']['pending_quarantined'] == 1
    assert result['approval_recovery_allowed'] is False


def change_json(path, **fields):
    value = json.loads(path.read_bytes())
    runtime_control.write_control_bytes_atomic(path, json.dumps({**value, **fields}).encode())


def project_notice_cooldown(root, monkeypatch):
    home = Path(os.environ['CODEX_HOME'])
    stamp = time.time()
    with sqlite3.connect(home / 'state_5.sqlite') as database:
        database.execute('CREATE TABLE threads (id TEXT,cwd TEXT,model_provider TEXT)')
        database.execute('INSERT INTO threads VALUES (?,?,?)', ('main-chat', str(root), 'openai'))
    with sqlite3.connect(home / 'thread_history_1.sqlite') as database:
        database.execute('CREATE TABLE thread_turns (thread_id TEXT,turn_id TEXT,error_json TEXT,completed_at REAL,status TEXT)')
        database.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?)',
            ('main-chat', 'main-turn', json.dumps({'code':'serverOverloaded'}), stamp, 'failed'))
    source = hashlib.sha256(b'main:main-chat:main-turn').hexdigest()
    cooldown = root / 'runtime/control/approval-guard/cooldown.json'
    change_json(cooldown, run_id=None, triggered_at=stamp, blocked_until=stamp + 3 * 60 * 60,
        sources=[source], notice_code='server_overloaded')
    return cooldown, home


def test_project_notice_source_sync_keeps_approval_closure_and_unowned_notice_separate(official_production, monkeypatch, capsys):
    root, original, action, _before, expected, _remote, _cron = official_production
    cooldown, _home = project_notice_cooldown(root, monkeypatch)
    unchanged = {p:(p.stat().st_ino,p.read_bytes()) for p in
        [cooldown, original.file, action.with_name('review-interruption.json'),
         root / ('runtime/control/approval-guard/runs/' + 'a' * 32 + '/closed-readonly.json')]}
    code, result = run_release(official_production, monkeypatch, capsys)
    assert code == 0 and result['status'] == 'synced' and result['after_revision'] == expected
    proof = result['original_execution']['official_stop_proof']
    assert proof['cooldown_attribution'] == 'project_notice'
    assert proof['cooldown']['run_id'] is None and proof['run_id'] == 'a' * 32
    assert len(proof['project_notices']) == 1
    assert result['approval_recovery_allowed'] is False and result['cooldown_preserved'] is True
    assert {p:(p.stat().st_ino,p.read_bytes()) for p in unchanged} == unchanged


@pytest.mark.parametrize('damage', ['source','code','time','cwd','unreadable_history','overlaps_original','foreign_run'])
def test_unowned_notice_requires_actual_project_terminal_error(official_production, monkeypatch, capsys, damage):
    root, _original, _action, before, _expected, _remote, _cron = official_production
    cooldown, home = project_notice_cooldown(root, monkeypatch)
    if damage == 'source':
        change_json(cooldown, sources=['0' * 64])
    elif damage == 'foreign_run':
        change_json(cooldown, run_id='d' * 32)
    elif damage == 'cwd':
        with sqlite3.connect(home / 'state_5.sqlite') as database:
            database.execute('UPDATE threads SET cwd=?', ('other-project',))
    elif damage == 'unreadable_history':
        (home / 'thread_history_1.sqlite').unlink()
    elif damage == 'overlaps_original':
        value = json.loads(cooldown.read_bytes())
        change_json(cooldown, triggered_at=value['triggered_at'] - 100, blocked_until=value['blocked_until'] - 100)
    else:
        with sqlite3.connect(home / 'thread_history_1.sqlite') as database:
            if damage == 'code':
                database.execute('UPDATE thread_turns SET error_json=?', (json.dumps({'code':'rateLimitExceeded'}),))
            else:
                database.execute('UPDATE thread_turns SET completed_at=completed_at-1')
    actual = cooldown.read_bytes()
    code, result = run_release(official_production, monkeypatch, capsys)
    assert code == 1 and result['status'] == 'blocked'
    assert git(root, 'rev-parse', 'HEAD') == before and cooldown.read_bytes() == actual


@pytest.mark.parametrize('scenario', [
    'no_explicit_mode', 'no_stopped_mode', 'conflicting_modes', 'manual_stop', 'technical_stop',
    'active_cron', 'wrong_model', 'environment_stop', 'wrong_session', 'unknown_exit', 'stdout_changed',
    'wrong_cooldown_run', 'bad_cooldown_duration', 'missing_cooldown', 'missing_closed_run', 'wrong_closed_cause',
    'missing_interruption', 'missing_manual_receipt', 'missing_archived_action', 'writer_artifact', 'unknown_write',
    'other_continuation', 'unexplained_count', 'missing_count_proof', 'untrusted_identity', 'bad_policy', 'bad_freeze',
    'active_readonly_group', 'launch_intent',
])
def test_public_cli_refuses_unsafe_official_source_maintenance_without_sync(official_production, monkeypatch, capsys, scenario):
    root, original, action, before, _expected, _remote, cron = official_production
    run = root / ('runtime/control/approval-guard/runs/' + 'a' * 32)
    cooldown = root / 'runtime/control/approval-guard/cooldown.json'
    process = None
    if scenario in {'manual_stop', 'technical_stop'}:
        reason = 'manual' if scenario == 'manual_stop' else 'codex_review_technical_failure'
        runtime_control.write_control_bytes_atomic(root / 'runtime/stop-preaudit.flag', ('stop_requested:' + reason + '\n').encode())
    elif scenario == 'active_cron':
        cron.write_text(cron.read_text().replace('PAUSED', 'ACTIVE'))
    elif scenario == 'wrong_model':
        cron.write_text(cron.read_text().replace('gpt-6.1-sol', 'wrong-model'))
    elif scenario == 'environment_stop':
        monkeypatch.setenv('OA_FINANCE_PREAUDIT_STOP_REQUESTED', '1')
    elif scenario == 'wrong_session':
        change_json(original.file.with_name('tool-session.json'), tool_session_id='different-session')
    elif scenario == 'unknown_exit':
        change_json(original.file, state='waiting', exit_code=None)
    elif scenario == 'stdout_changed':
        original.file.with_name('stdout.log').write_bytes(b'not original output')
    elif scenario == 'wrong_cooldown_run':
        change_json(cooldown, run_id='d' * 32)
    elif scenario == 'bad_cooldown_duration':
        change_json(cooldown, blocked_until=0)
    elif scenario == 'missing_cooldown':
        cooldown.unlink()
    elif scenario == 'missing_closed_run':
        (run / 'closed-readonly.json').unlink()
    elif scenario == 'wrong_closed_cause':
        change_json(run / 'closed-readonly.json', cause='controller_orphaned')
    elif scenario.startswith('missing_') and scenario in {'missing_interruption', 'missing_manual_receipt', 'missing_archived_action'}:
        action.with_name({'missing_interruption': 'review-interruption.json', 'missing_manual_receipt': 'manual-review-receipt.json',
            'missing_archived_action': 'interrupted-action.json'}[scenario]).unlink()
    elif scenario == 'writer_artifact':
        runtime_control.write_control_bytes_atomic(action.with_name('writer-boundary.json'), b'{}')
    elif scenario == 'unknown_write':
        change_json(action.with_name('state.json'), write_state='unknown')
    elif scenario == 'active_readonly_group':
        from apps.oa_finance_preaudit import approval_guard
        process = subprocess.Popen(['sleep', '30'], start_new_session=True)
        progress = json.loads((run / 'progress.json').read_bytes())
        runtime_control.write_control_bytes_atomic(run / ('pool-' + 'f' * 32 + '.json'), json.dumps({
            'kind': 'approval_readonly_pool', 'schema_version': 1, 'run_id': 'a' * 32,
            'controller_instance_id': 'b' * 32, 'controller': progress['controller'], 'role': 'review',
            'pool_id': 'f' * 32, 'state': 'registered', 'processes': [approval_guard.process_identity(process.pid)],
        }).encode())
    elif scenario == 'launch_intent':
        runtime_control.write_control_bytes_atomic(run / ('pool-' + 'f' * 32 + '.json'), b'{"state":"launching"}')
    def live(target):
        snapshot = offline_live(target, original)
        if scenario == 'other_continuation':
            snapshot['reconciliation']['continuation'].update(status='blocked', blocking_reasons={'pending_failure_not_isolated': 1})
        elif scenario == 'unexplained_count':
            snapshot['reconciliation']['continuation']['isolated_pending'] = 3
        elif scenario == 'missing_count_proof':
            snapshot['maintenance_current_inputs'].pop('uncounted_interrupted_actions')
        elif scenario == 'untrusted_identity':
            snapshot['live_check']['oa']['execution_identity'] = 'mismatch'
        elif scenario == 'bad_policy':
            snapshot['maintenance_current_inputs']['authorized_workflows'] = []
        elif scenario == 'bad_freeze':
            snapshot['maintenance_current_inputs']['validated_frozen_count'] = 0
        return snapshot
    try:
        code, result = run_release(official_production, monkeypatch, capsys, live=live,
            mode=scenario != 'no_explicit_mode', stopped=scenario != 'no_stopped_mode',
            options=['--supervisor-loss-source-only', 'YES'] if scenario == 'conflicting_modes' else [])
        assert code == 1 and result['status'] == 'blocked'
        assert result['source_sync_state'] == 'not_updated'
        assert result['next_process_ready'] is False and result['write_state'] == 'not_requested'
        assert git(root, 'rev-parse', 'HEAD') == before
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.parametrize('control', ['stop', 'cooldown', 'cron', 'archived_action', 'review_failure',
    'successful_receipt', 'successful_decision', 'successful_remark', 'writer_boundary_created', 'submission_envelope_created'])
@pytest.mark.parametrize('after_sync', [False, True])
def test_cli_reports_actual_update_when_preserved_evidence_is_replaced(official_production, monkeypatch, capsys, control, after_sync):
    root, original, action, before, expected, _remote, cron = official_production
    paths = {'stop': root / 'runtime/stop-preaudit.flag', 'cooldown': root / 'runtime/control/approval-guard/cooldown.json',
        'cron': cron, 'archived_action': action.with_name('interrupted-action.json')}
    failure = next((root / 'runtime/finance_preaudit_flows').glob('*/cases/*/attempt-1/review-failure.json'))
    completed = next((root / 'runtime/finance_preaudit_flows').glob('*/cases/*/attempt-1/execution-receipt.json'))
    paths.update(review_failure=failure, successful_receipt=completed,
        successful_decision=completed.with_name('review-decision.json'), successful_remark=completed.with_name('remark.txt'),
        writer_boundary_created=completed.with_name('writer-boundary.json'),
        submission_envelope_created=completed.with_name('submission-envelope.json'))
    calls = 0
    def live(target):
        nonlocal calls
        calls += 1
        snapshot = offline_live(target, original)
        if calls == (2 if after_sync else 1):
            path = paths[control]
            runtime_control.write_control_bytes_atomic(path, path.read_bytes() if path.exists() else b'{}')
        return snapshot
    code, result = run_release(official_production, monkeypatch, capsys, live=live)
    assert code == 1 and result['status'] == 'blocked'
    assert result['source_sync_state'] == ('updated' if after_sync else 'not_updated')
    assert git(root, 'rev-parse', 'HEAD') == (expected if after_sync else before)
    assert result['original_evidence_preserved'] == 'unknown'
    if control == 'cooldown':
        assert result['cooldown_preserved'] is False


@pytest.mark.parametrize('damage', ['session', 'output', 'closed_run', 'unknown_exit'])
def test_original_readback_refuses_untrusted_official_evidence(official_production, damage):
    root, original, _action, _before, _expected, _remote, _cron = official_production
    if damage == 'session':
        change_json(original.file.with_name('tool-session.json'), tool_session_id='another-session')
    elif damage == 'output':
        original.file.with_name('stdout.log').write_bytes(b'changed output')
    elif damage == 'closed_run':
        (root / ('runtime/control/approval-guard/runs/' + 'a' * 32 + '/closed-readonly.json')).unlink()
    else:
        change_json(original.file, state='waiting', exit_code=None)
    with pytest.raises(RuntimeError, match='maintenance_original_execution_untrusted'):
        maintenance_runtime.read_original_execution(root, original, official_stop_source_only=True)
