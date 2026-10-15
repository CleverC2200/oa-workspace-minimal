from __future__ import annotations

import json
import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import app_review_shutdown as shutdown, controller_handoff, host_chat_review as host
from tests.finance_preaudit.test_host_chat_review import exchange, native_call, seal_execution


def proofs(owner, child, *, status='interrupted', exit_code=0, session='original-session', wait_arguments=None):
    parent = child.with_name('parent.jsonl')
    interrupts = [native_call(parent, 'interrupt_agent', {'target': f'/root/reviewer_{slot}'},
        f'stop-{slot}', {'previous_status': 'running'}) for slot in range(3)]
    statuses = native_call(parent, 'list_agents', {}, 'shutdown-status', {'agents': [
        {'agent_name': f'/root/reviewer_{slot}', 'agent_status': status} for slot in range(3)]})
    wait = native_call(parent, 'functions.exec',
        'text(await tools.write_stdin(' + (wait_arguments or json.dumps({'session_id': session, 'chars': '', 'yield_time_ms': 1000})) + '));',
        'original-driver-wait', {'exit_code': exit_code, 'output': 'test driver exited'})
    return {'source': parent, 'interrupt_lines': interrupts, 'status_line': statuses, 'wait_line': wait}


@pytest.mark.parametrize('reason', ['completed', 'cancelled', 'reviewer_failed', 'parent_lost'])
def test_all_exit_paths_need_three_interrupts_status_and_original_wait(exchange, reason):
    _, _, owner, child, _, manifest, _ = exchange
    plan = shutdown.begin(owner=owner, reason=reason)
    assert len(plan['interrupt_targets']) == 3
    assert plan['status'] == 'pending'
    assert shutdown.begin(owner=owner, reason=reason) == plan
    with pytest.raises(ValueError, match='parent_lost_or_closed'):
        host.registered_reviewers(owner=owner)
    with pytest.raises(ValueError, match='shutdown_incomplete'):
        shutdown.finish(owner=owner, **proofs(owner, child))
    assert json.loads(host.registry_file(owner).read_text())['shutdown']['status'] == 'pending'
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    result = shutdown.finish(owner=owner, **proofs(owner, child))
    assert result['status'] == 'verified'
    assert result['reason'] == reason
    assert result['exchange_state'] == ('closed' if reason == 'completed' else 'cancelled')
    assert result['driver_exit_code'] == 0
    assert not runtime_control.writer_active()
    assert shutdown.finish(owner=owner, **proofs(owner, child)) == result


@pytest.mark.parametrize('damage', ['missing_interrupt', 'duplicate_interrupt', 'wrong_target', 'running', 'wrong_session',
    'still_waiting', 'forged_stdout', 'mixed_commands', 'wrong_exit', 'stale_status', 'writer_active', 'missing_child', 'tool_error', 'status_error'])
def test_shutdown_evidence_failures_remain_pending(exchange, monkeypatch, damage):
    _, _, owner, child, _, manifest, _ = exchange
    shutdown.begin(owner=owner, reason='cancelled')
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    args = proofs(owner, child, status='running' if damage == 'running' else 'interrupted',
        session='another-session' if damage == 'wrong_session' else 'original-session', exit_code=1 if damage == 'wrong_exit' else 0)
    if damage == 'missing_interrupt': args['interrupt_lines'] = args['interrupt_lines'][:2]
    if damage == 'duplicate_interrupt': args['interrupt_lines'][2] = args['interrupt_lines'][0]
    if damage == 'stale_status': args['status_line'] = args['interrupt_lines'][0]
    if damage in {'wrong_target', 'still_waiting', 'forged_stdout', 'mixed_commands'}:
        rows = args['source'].read_text().splitlines()
        index = args['interrupt_lines'][0] - 1 if damage == 'wrong_target' else args['wait_line'] - 1
        row = json.loads(rows[index])
        if damage == 'wrong_target': row['payload']['arguments'] = json.dumps({'target': '/root/unrelated'})
        elif damage == 'mixed_commands': row['payload']['input'] += '\ntext(await tools.exec_command({"cmd":"true"}));'
        else:
            index += 1; row = json.loads(rows[index])
            output = json.dumps({'exit_code': 0, 'output': 'pretend success'}) if damage == 'forged_stdout' else 'still running'
            row['payload']['output'] = json.dumps({'session_id': 123, 'output': output})
        rows[index] = json.dumps(row); args['source'].write_text('\n'.join(rows) + '\n')
    if damage in {'missing_child', 'tool_error', 'status_error'}:
        rows = args['source'].read_text().splitlines()
        index = args['interrupt_lines'][0] if damage == 'tool_error' else args['status_line']
        row = json.loads(rows[index]); output = json.loads(row['payload']['output'])
        if damage == 'missing_child': output['agents'].pop()
        else: output['isError'] = True
        row['payload']['output'] = json.dumps(output); rows[index] = json.dumps(row)
        args['source'].write_text('\n'.join(rows) + '\n')
    if damage == 'writer_active': monkeypatch.setattr(runtime_control, 'writer_active', lambda: True)
    with pytest.raises(ValueError): shutdown.finish(owner=owner, **args)
    assert json.loads(host.registry_file(owner).read_text())['shutdown']['status'] == 'pending'


def test_shutdown_cannot_be_reported_without_begin(exchange):
    _, _, owner, child, *_ = exchange
    with pytest.raises(ValueError, match='shutdown_not_started'):
        shutdown.finish(owner=owner, **proofs(owner, child))


def test_shutdown_preserves_writer_and_rejects_reason_rebinding(exchange, monkeypatch):
    _, _, owner, *_ = exchange
    monkeypatch.setattr(runtime_control, 'writer_active', lambda: True)
    assert shutdown.begin(owner=owner, reason='reviewer_failed')['status'] == 'pending'
    with pytest.raises(ValueError, match='reason_conflict'):
        shutdown.begin(owner=owner, reason='completed')


def test_abort_can_verify_nonzero_driver_exit_without_reporting_business_success(exchange):
    _, _, owner, child, _, manifest, _ = exchange
    shutdown.begin(owner=owner, reason='reviewer_failed')
    controller_handoff.finish(manifest, status='failed', children_reaped=True, health='degraded')
    path = seal_execution(owner, manifest)
    receipt = json.loads(path.read_text())
    receipt.update(result='failed', exit_code=1, controller_state='failed')
    host.guard.write(path, receipt)
    result = shutdown.finish(owner=owner, **proofs(owner, child, exit_code=1))
    assert result['status'] == 'verified'
    assert result['exchange_state'] == 'cancelled' and result['driver_exit_code'] == 1


@pytest.mark.parametrize('arguments', [
    '{session_id:"original-session",chars:"",yield_time_ms:1000,max_output_tokens:3000}',
    '{"session_id":"original-session", chars:"", "yield_time_ms":1000}',
])
def test_shutdown_accepts_native_literal_keys_and_preserves_original_sources(exchange, arguments):
    _, _, owner, child, _, manifest, _ = exchange
    shutdown.begin(owner=owner, reason='reviewer_failed')
    controller_handoff.finish(manifest, status='failed', children_reaped=True, health='degraded')
    path = seal_execution(owner, manifest)
    receipt = json.loads(path.read_text())
    receipt.update(result='failed', exit_code=1, controller_state='failed')
    host.guard.write(path, receipt)
    args = proofs(owner, child, exit_code=1, wait_arguments=arguments)
    original = args['source'].read_bytes()
    result = shutdown.finish(owner=owner, **args)
    assert result['status'] == 'verified' and result['driver_exit_code'] == 1
    assert result['driver_wait_source']['line'] == args['wait_line']
    assert args['source'].read_bytes() == original


@pytest.mark.parametrize('arguments', [
    '{session_id:session,chars:""}',
    '{session_id:"original-"+"session",chars:""}',
    '{...other,session_id:"original-session",chars:""}',
    '{["session_id"]:"original-session",chars:""}',
    '{session_id:"original-session",session_id:"original-session",chars:""}',
    '{"session_id":"original-session","session_id":"original-session",chars:""}',
    '{session_id:"another-session",chars:""}',
    '{session_id:"original-session",chars:"x"}',
    '{session_id:"original-session",chars:"",extra:0}',
    '{session_id:"original-session",chars:"",yield_time_ms:call()}',
    '{session_id:"original-session",chars:"",yield_time_ms:{value:1000}}',
])
def test_shutdown_rejects_nonliteral_or_ambiguous_wait_arguments(exchange, arguments):
    _, _, owner, child, _, manifest, _ = exchange
    shutdown.begin(owner=owner, reason='reviewer_failed')
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    with pytest.raises(ValueError):
        shutdown.finish(owner=owner, **proofs(owner, child, wait_arguments=arguments))
    assert json.loads(host.registry_file(owner).read_text())['shutdown']['status'] == 'pending'


def test_old_interrupts_cannot_be_reused_for_a_new_shutdown(exchange):
    _, _, owner, child, *_ = exchange
    args = proofs(owner, child)
    shutdown.begin(owner=owner, reason='cancelled')
    with pytest.raises(ValueError, match='stale_source'):
        shutdown.finish(owner=owner, **args)


@pytest.mark.parametrize('damage', [None, 'wrong_session', 'missing_result', 'extra_result',
    'running_command', 'running_wait', 'wrong_exit', 'dynamic_command', 'duplicate_command_key',
    'extra_wait', 'trailing_command'])
def test_cli_shutdown_binds_wait_after_literal_command(exchange, damage):
    _, _, owner, child, _, manifest, _ = exchange
    shutdown.begin(owner=owner, reason='reviewer_failed')
    controller_handoff.finish(manifest, status='failed', children_reaped=True, health='degraded')
    path = seal_execution(owner, manifest)
    receipt = json.loads(path.read_text())
    receipt.update(result='failed', exit_code=1, controller_state='failed')
    host.guard.write(path, receipt)
    args = proofs(owner, child, exit_code=1)
    command = 'text(await tools.exec_command({cmd:"heartbeat",workdir:"/offline",max_output_tokens:2200}));\n'
    wait = 'text(await tools.write_stdin({session_id:"original-session",chars:"",yield_time_ms:1000}));'
    results = [{'exit_code': 0, 'output': 'heartbeat returned'}, {'exit_code': 1, 'output': 'original driver exited'}]
    if damage is None:
        results[0]['exit_code'] = 1  # A failed heartbeat must not hide the actual nonzero wait.
    elif damage == 'wrong_session': wait = wait.replace('original-session', 'another-session')
    elif damage == 'missing_result': results.pop()
    elif damage == 'extra_result': results.append({'exit_code': 1, 'output': 'unrelated'})
    elif damage == 'running_command': results[0] = {'session_id': 123, 'output': 'running'}
    elif damage == 'running_wait': results[1] = {'session_id': 'original-session', 'output': 'running'}
    elif damage == 'wrong_exit': results[1]['exit_code'] = 0
    elif damage == 'dynamic_command': command = 'text(await tools.exec_command(command));\n'
    elif damage == 'duplicate_command_key': command = command.replace('cmd:"heartbeat"', 'cmd:"heartbeat",cmd:"other"')
    elif damage == 'extra_wait': wait += wait
    elif damage == 'trailing_command': wait += command
    rows = args['source'].read_text().splitlines()
    call = json.loads(rows[args['wait_line'] - 1])
    call['payload']['input'] = command + wait
    rows[args['wait_line'] - 1] = json.dumps(call)
    output = json.loads(rows[args['wait_line']])
    output['payload']['output'] = [{'type': 'input_text', 'text': 'Script completed'},
        *[{'type': 'input_text', 'text': json.dumps(result)} for result in results]]
    rows[args['wait_line']] = json.dumps(output)
    args['source'].write_text('\n'.join(rows) + '\n')
    original = args['source'].read_bytes()
    cli = ['--operation', 'finish-shutdown', '--owner-task-id', owner,
        '--source-session-file', str(args['source']), '--source-line', str(args['status_line']),
        '--wait-line', str(args['wait_line'])]
    for line in args['interrupt_lines']:
        cli.extend(['--interrupt-line', str(line)])
    if damage is None:
        assert host.main(cli) == 0
        sealed = json.loads(host.registry_file(owner).read_text())['shutdown']
        assert sealed['status'] == 'verified'
        assert sealed['receipt']['driver_exit_code'] == 1
        assert sealed['receipt']['driver_wait_source']['line'] == args['wait_line']
    else:
        with pytest.raises(ValueError): host.main(cli)
        assert json.loads(host.registry_file(owner).read_text())['shutdown']['status'] == 'pending'
    assert args['source'].read_bytes() == original


def test_cli_close_cannot_skip_unified_shutdown_proofs(exchange):
    _, _, owner, *_ = exchange
    with pytest.raises(SystemExit) as error:
        host.main(['--operation', 'close', '--owner-task-id', owner])
    assert error.value.code == 2


def test_completed_shutdown_cannot_accept_an_exchange_cancelled_during_cleanup(exchange):
    _, _, owner, child, _, manifest, _ = exchange
    shutdown.begin(owner=owner, reason='completed')
    host.close(owner=owner, reason='parent_lost')
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    with pytest.raises(ValueError, match='reason_conflict'):
        shutdown.finish(owner=owner, **proofs(owner, child))
    assert json.loads(host.registry_file(owner).read_text())['shutdown']['status'] == 'pending'
    assert shutdown.begin(owner=owner, reason='parent_lost')['reason'] == 'parent_lost'
    assert shutdown.finish(owner=owner, **proofs(owner, child))['reason'] == 'parent_lost'
