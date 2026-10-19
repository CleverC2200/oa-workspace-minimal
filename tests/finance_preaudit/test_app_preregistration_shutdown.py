from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import subprocess
from uuid import uuid4

import pytest

from apps.oa_finance_preaudit import app_review_shutdown as shutdown, host_chat_review as host
from scripts import finance_preaudit_run as runner
from tests.finance_preaudit.test_app_reviewer_slots import native_sessions
from tests.finance_preaudit.test_host_chat_review import native_call


@pytest.fixture
def failed_start(tmp_path, monkeypatch):
    owner, parent, reviewers = native_sessions(tmp_path, monkeypatch)
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'control/controller.lock'))
    monkeypatch.setenv(host.TRANSPORT_ENV, 'host_chat')
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    directory = tmp_path / 'control/executions' / uuid4().hex
    command = [sys.executable, '-c', '''import json
from pathlib import Path
from apps.oa_approval import runtime_control as control
from apps.oa_finance_preaudit import controller_handoff as handoff,approval_lifecycle as lifecycle
with control.global_controller_lease():
    manifest=handoff.begin({'review_configuration':__import__('apps.oa_finance_preaudit.host_chat_review',fromlist=['IDENTITY']).IDENTITY})
    with lifecycle.run_scope(manifest,Path.cwd()):pass
    handoff.finish(manifest,status='failed',children_reaped=True,health='degraded')
print(json.dumps({'kind':'preaudit_production_rolling_drain','status':'failed','completed':0,'process_attempts':0,
 'peak_active':0,'prepare_active_seconds':0,'processes_reaped':True,'pipeline':{'writer':{'max_concurrency':0}}}))
raise SystemExit(1)
''']
    script = f"from pathlib import Path\nfrom scripts.finance_preaudit_run import supervise\nsupervise({command!r},directory=Path({str(directory)!r}),owner_task_id={owner!r})"
    subprocess.run([sys.executable, "-c", script], cwd=runner.PROJECT_ROOT, check=True, capture_output=True)
    result = json.loads((directory / "execution.json").read_text())
    runner.bind_tool_session(directory / 'execution.json', owner_task_id=owner, session_id='native-session')
    receipt = json.loads((directory / 'execution.json').read_text())
    for key in ['result', 'output_sha256', 'manifest_sha256']:
        receipt.pop(key)
    receipt.update(run_id=None, controller_instance_id=None, controller_state='unknown', children_reaped='unknown',
                   stop_wait_error='controller_stop_timeout')
    receipt.pop('code_revision', None)
    host.guard.write(directory / 'execution.json', receipt)
    rows = [json.loads(row) for row in parent.read_text().splitlines()]
    for row in rows[1:]:
        row['timestamp'] = datetime.now(timezone.utc).isoformat()
    parent.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    interrupts = [native_call(parent, 'interrupt_agent', {'target': f'/root/reviewer_{slot}'},
                             f'interrupt-{slot}', {'previous_status': 'running'}) for slot in range(3)]
    statuses = native_call(parent, 'list_agents', {}, 'idle-status', {'agents': [
        {'agent_name': f'/root/reviewer_{slot}', 'agent_status': 'interrupted'} for slot in range(3)]})
    wait = native_call(parent, 'functions.exec',
                      'text(await tools.write_stdin({session_id:"native-session",chars:"",yield_time_ms:1000}));',
                      'terminal-wait', {'exit_code': 1, 'output': 'offline driver exited'})
    return {'owner': owner, 'source': parent, 'reviewers': reviewers, 'receipt_file': directory / 'execution.json',
            'invocation_id': directory.name, 'tool_session_id': 'native-session',
            'interrupt_lines': interrupts, 'status_line': statuses, 'wait_line': wait}, result


def test_pre_registration_shutdown_seals_native_exit_without_rewriting_partial_execution(failed_start, capsys):
    args, result = failed_start
    original = args['receipt_file'].read_bytes()
    partial = json.loads(original)
    assert partial['run_id'] is None and partial['controller_state'] == 'unknown' and partial['children_reaped'] == 'unknown'
    projected = runner.read_execution(args['receipt_file'], owner_task_id=args['owner'], invocation_id=args['invocation_id'])
    assert projected['controller_state'] == 'failed' and projected['children_reaped'] is True
    assert projected['terminal'] is False
    with pytest.raises(ValueError, match='previous_round_incomplete'):
        host.assert_previous_rounds_closed(owner=str(uuid4()))
    command = ['--operation', 'finish-unregistered-shutdown', '--owner-task-id', args['owner'],
               '--receipt-file', str(args['receipt_file']), '--invocation-id', args['invocation_id'],
               '--tool-session-id', args['tool_session_id'], '--source-session-file', str(args['source']),
               '--source-line', str(args['status_line']), '--wait-line', str(args['wait_line'])]
    for reviewer, interrupt in zip(args['reviewers'], args['interrupt_lines']):
        command.extend(['--reviewer-session-file', reviewer['session_file'], '--spawn-line', str(reviewer['spawn_line']),
                        '--interrupt-line', str(interrupt)])
    assert host.main(command) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status'] == 'verified' and receipt['driver_exit_code'] == 1
    assert receipt['run_id'] == result['run_id'] and receipt['no_actions_or_writes'] is True
    assert args['receipt_file'].read_bytes() == original
    host.assert_previous_rounds_closed(owner=str(uuid4()))
    report = runner.read_execution(args['receipt_file'], owner_task_id=args['owner'],
                                   invocation_id=args['invocation_id'], tool_session_id='native-session')
    assert report['status'] == 'blocked' and report['terminal'] is False
    assert shutdown.finish_unregistered(**args) == receipt


@pytest.mark.parametrize('damage', ['wrong_child', 'wrong_target', 'wrong_session', 'wrong_exit',
    'missing_interrupt', 'status_before_interrupt', 'stale_wait', 'stale_spawn', 'actions', 'writer',
    'process_attempts', 'completed_work', 'missing_progress', 'registered_round', 'changed_after_seal',
    'wrong_signal', 'reversed_manifest_clock'])
def test_pre_registration_shutdown_rejects_mismatched_or_work_bearing_evidence(failed_start, damage):
    args, result = failed_start
    original = args['receipt_file'].read_bytes()
    if damage == 'changed_after_seal':
        shutdown.finish_unregistered(**args)
    rows = args['source'].read_text().splitlines()
    if damage == 'wrong_child':
        child = Path(args['reviewers'][0]['session_file'])
        row = json.loads(child.read_text())
        row['payload']['source']['subagent']['thread_spawn']['parent_thread_id'] = str(uuid4())
        child.write_text(json.dumps(row) + '\n')
    elif damage in {'wrong_target', 'wrong_session', 'wrong_exit', 'stale_wait', 'stale_spawn'}:
        line = args['interrupt_lines'][0] if damage == 'wrong_target' else args['wait_line']
        if damage == 'stale_spawn':
            line = args['reviewers'][0]['spawn_line']
        index = line if damage == 'wrong_exit' else line - 1
        row = json.loads(rows[index])
        if damage == 'wrong_target':
            row['payload']['arguments'] = json.dumps({'target': '/root/other'})
        elif damage == 'wrong_session':
            row['payload']['input'] = row['payload']['input'].replace('native-session', 'other-session')
        elif damage == 'wrong_exit':
            row['payload']['output'] = json.dumps({'exit_code': 0, 'output': 'different driver'})
        else:
            row['timestamp'] = '2020-01-01T00:00:00+00:00'
        rows[index] = json.dumps(row)
        args['source'].write_text('\n'.join(rows) + '\n')
    elif damage == 'missing_interrupt':
        args['interrupt_lines'] = args['interrupt_lines'][:2]
    elif damage == 'status_before_interrupt':
        args['status_line'] = args['interrupt_lines'][0]
    elif damage in {'actions', 'writer', 'missing_progress', 'changed_after_seal'}:
        from apps.oa_finance_preaudit import approval_lifecycle
        path = approval_lifecycle.run_directory(result['run_id']) / 'progress.json'
        progress = json.loads(path.read_text())
        if damage == 'missing_progress':
            path.unlink()
        else:
            if damage == 'actions':
                progress['actions'] = [str(runner.PROJECT_ROOT / 'runtime/finance_preaudit_flows/x/action.json')]
            elif damage == 'writer':
                progress['writer_active'] = True
            else:
                progress['heartbeat_at'] += .000001
            host.guard.write(path, progress)
    elif damage == 'registered_round':
        host.guard.write(host.registry_file(args['owner'], result['run_id']), {'state': 'open'})
    elif damage == 'wrong_signal':
        receipt = json.loads(args['receipt_file'].read_text())
        receipt['exit_signal'] = 9
        host.guard.write(args['receipt_file'], receipt)
        original = args['receipt_file'].read_bytes()
    elif damage == 'reversed_manifest_clock':
        path = args['receipt_file'].with_name('controller-manifest.json')
        manifest = json.loads(path.read_text())
        manifest['completed_at'] = json.loads(original)['started_at']
        host.guard.write(path, manifest)
    else:
        path = args['receipt_file'].with_name('stdout.log')
        payload = json.loads(path.read_text())
        payload['process_attempts' if damage == 'process_attempts' else 'completed'] = 1
        path.write_text(json.dumps(payload))
    with pytest.raises((ValueError, FileNotFoundError)):
        shutdown.finish_unregistered(**args)
    assert args['receipt_file'].read_bytes() == original
    if damage != 'changed_after_seal':
        assert not args['receipt_file'].with_name('pre-registration-shutdown.json').exists()


def test_sealed_pre_registration_history_can_be_read_while_current_writer_is_active(failed_start, monkeypatch):
    args, _ = failed_start
    saved = shutdown.finish_unregistered(**args)
    monkeypatch.setattr(shutdown.runtime_control, 'writer_active', lambda: True)
    assert shutdown.sealed_unregistered(args['receipt_file']) == saved
    with pytest.raises(ValueError, match='previous_round_incomplete'):
        host.assert_previous_rounds_closed(owner=str(uuid4()))


def test_public_cli_provider_switch_keeps_original_app_closure_gate(failed_start, monkeypatch, capsys):
    args, _ = failed_start
    original = args['receipt_file'].read_bytes()
    monkeypatch.setattr(runner.auth, 'load_local_env', lambda: None)
    monkeypatch.setattr(runner.approval_guard, 'observe_project_errors', lambda _: None)
    assert runner.approval_lifecycle.startup(runner.PROJECT_ROOT)['status'] == 'ready'
    monkeypatch.setattr(runner, 'supervise', lambda *_args, **_options: pytest.fail('unclosed App round launched'))
    assert runner.main(['--owner-task-id', str(uuid4()), '--review-provider', 'deepseek', '--', '--rolling']) == 1
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result['primary_failure'] == {'error_type': 'ValueError', 'error_code': 'host_review_previous_round_incomplete',
                                         'phase': 'app_round_closure'}
    assert args['receipt_file'].read_bytes() == original
