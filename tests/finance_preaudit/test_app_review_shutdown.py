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


@pytest.mark.parametrize('render', ['text(r);', 'text(JSON.stringify(r));'])
def test_shutdown_accepts_sole_wait_result_variable(exchange, render):
    _, _, owner, child, _, manifest, _ = exchange
    shutdown.begin(owner=owner, reason='reviewer_failed')
    controller_handoff.finish(manifest, status='failed', children_reaped=True, health='degraded')
    path = seal_execution(owner, manifest)
    receipt = json.loads(path.read_text())
    receipt.update(result='failed', exit_code=1, controller_state='failed')
    host.guard.write(path, receipt)
    args = proofs(owner, child, exit_code=1)
    args['wait_line'] = native_call(args['source'], 'functions.exec',
        'const r = await tools.write_stdin({session_id:"original-session",chars:"",yield_time_ms:1000}); ' + render,
        'wrapped-original-wait', {'exit_code': 1, 'output': 'driver exited'})
    original = args['source'].read_bytes()
    result = shutdown.finish(owner=owner, **args)
    assert result['driver_exit_code'] == 1 and result['status'] == 'verified'
    assert args['source'].read_bytes() == original


@pytest.mark.parametrize('code', [
    'const r = await tools.write_stdin({session_id:"original-session"}); text(JSON.stringify(other));',
    'const r = await tools.write_stdin({session_id:"original-session"}); r.exit_code=0; text(r);',
    'const r = await tools.write_stdin({session_id:"original-session"}); text({...r,exit_code:0});',
    'const r = await tools.write_stdin({session_id:"other-session"}); text(JSON.stringify(r));',
    'const r = {exit_code:0,output:"fake"}; text(JSON.stringify(r));',
])
def test_shutdown_rejects_changed_or_unbound_wait_variable(exchange, code):
    _, _, owner, child, _, manifest, _ = exchange
    shutdown.begin(owner=owner, reason='reviewer_failed')
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    args = proofs(owner, child)
    args['wait_line'] = native_call(args['source'], 'functions.exec', code,
        'untrusted-wait', {'exit_code': 0, 'output': 'driver exited'})
    with pytest.raises(ValueError):
        shutdown.finish(owner=owner, **args)
    assert json.loads(host.registry_file(owner).read_text())['shutdown']['status'] == 'pending'


@pytest.mark.parametrize('compressed', [False, True])
def test_shutdown_reads_archived_native_sources_without_rewriting_proofs(exchange, compressed):
    from pathlib import Path
    import os
    import subprocess
    _, _, owner, child, _, manifest, _ = exchange
    shutdown.begin(owner=owner, reason='reviewer_failed')
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    args = proofs(owner, child)
    archive = Path(os.environ['CODEX_HOME']) / 'archived_sessions'
    archive.mkdir()
    original = {}
    for source in child.parent.glob('*.jsonl'):
        original[source.name] = source.read_bytes()
        source.rename(archive / source.name)
        if compressed:
            target = archive / (source.name + '.zst')
            subprocess.run(['zstd', '-q', str(archive / source.name), '-o', str(target)], check=True)
            (archive / source.name).unlink()
    archives = {path: path.read_bytes() for path in archive.iterdir()}
    result = shutdown.finish(owner=owner, **args)
    assert result['status'] == 'verified'
    assert result['driver_wait_source']['session_file'] == str(args['source'])
    assert {path: path.read_bytes() for path in archive.iterdir()} == archives
    if not compressed:
        assert all((archive / name).read_bytes() == raw for name, raw in original.items())
    saved = host.registry_file(owner).read_bytes()
    assert shutdown.sealed_round(owner=owner, run_id=manifest['run_id'])['shutdown']['status'] == 'verified'
    assert host.registry_file(owner).read_bytes() == saved


@pytest.mark.parametrize('damage', ['wrong_session', 'changed_call', 'symlink', 'writable', 'existing_invalid_source'])
@pytest.mark.parametrize('compressed', [False, True])
def test_shutdown_rejects_untrusted_archived_native_sources(exchange, damage, compressed):
    from pathlib import Path
    import os
    import subprocess
    _, _, owner, child, _, manifest, _ = exchange
    shutdown.begin(owner=owner, reason='reviewer_failed')
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    args = proofs(owner, child)
    archive = Path(os.environ['CODEX_HOME']) / 'archived_sessions'
    archive.mkdir()
    archived = archive / args['source'].name
    args['source'].rename(archived)
    if damage in {'wrong_session', 'changed_call'}:
        rows = archived.read_text().splitlines()
        index = 0 if damage == 'wrong_session' else 1
        record = json.loads(rows[index])
        if damage == 'wrong_session':
            record['payload']['id'] = '00000000-0000-0000-0000-000000000000'
        else:
            record['payload']['arguments'] = json.dumps({'task_name': 'unrelated', 'message': 'different child'})
        rows[index] = json.dumps(record)
        archived.write_text('\n'.join(rows) + '\n')
    if compressed:
        target = archive / (archived.name + '.zst')
        subprocess.run(['zstd', '-q', str(archived), '-o', str(target)], check=True)
        archived.unlink()
        archived = target
    original = archived.read_bytes()
    if damage == 'symlink':
        target = archive / 'other.jsonl'
        archived.rename(target)
        archived.symlink_to(target)
    elif damage == 'writable':
        archived.chmod(0o666)
    elif damage == 'existing_invalid_source':
        args['source'].write_text('{}\n')
    with pytest.raises((ValueError, RuntimeError)):
        shutdown.finish(owner=owner, **args)
    assert json.loads(host.registry_file(owner).read_text())['shutdown']['status'] == 'pending'
    if damage not in {'wrong_session', 'changed_call'}:
        assert archived.read_bytes() == original


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
    'extra_wait', 'trailing_command', 'too_many_commands', 'interleaved_statement'])
@pytest.mark.parametrize('command_count', [1, 2])
def test_cli_shutdown_binds_wait_after_literal_command(exchange, damage, command_count, prior_poll=False):
    _, _, owner, child, _, manifest, _ = exchange
    shutdown.begin(owner=owner, reason='reviewer_failed')
    controller_handoff.finish(manifest, status='failed', children_reaped=True, health='degraded')
    path = seal_execution(owner, manifest)
    receipt = json.loads(path.read_text())
    receipt.update(result='failed', exit_code=1, controller_state='failed')
    host.guard.write(path, receipt)
    args = proofs(owner, child, exit_code=1)
    command = 'text(await tools.exec_command({cmd:"heartbeat {}",workdir:"/offline",max_output_tokens:2200}));\n'
    wait = 'text(await tools.write_stdin({session_id:"original-session",chars:"",yield_time_ms:1000}));'
    results = [{'exit_code': 0, 'output': 'heartbeat returned'}, {'exit_code': 1, 'output': 'original driver exited'}]
    if damage is None and not prior_poll:
        results[0]['exit_code'] = 1  # A failed heartbeat must not hide the actual nonzero wait.
    elif damage == 'wrong_session': wait = wait.replace('original-session', 'another-session')
    elif damage == 'missing_result': results.pop()
    elif damage == 'extra_result': results.append({'exit_code': 1, 'output': 'unrelated'})
    elif damage == 'running_command': results[0] = {'session_id': 123, 'output': 'running'}
    elif damage == 'running_wait': results[1] = {'session_id': 'original-session', 'output': 'running'}
    elif damage == 'wrong_exit': results[1]['exit_code'] = 0
    elif damage == 'dynamic_command': command = 'text(await tools.exec_command(command));\n'
    elif damage == 'duplicate_command_key': command = command.replace('cmd:"heartbeat {}"', 'cmd:"heartbeat {}",cmd:"other"')
    elif damage == 'extra_wait': wait += wait
    elif damage == 'trailing_command': wait += command
    if damage == 'interleaved_statement': command += 'text(1);\n'
    if damage == 'too_many_commands': command_count = 3
    results[:0] = [{'exit_code': 0, 'output': 'another query returned'}] * (command_count - 1)
    prefix = command * command_count
    if prior_poll:
        poll = 'text(await tools.write_stdin({session_id:"other-session",chars:"",yield_time_ms:1000}));\n'
        if damage == 'poll_same_session': poll = poll.replace('other-session', 'original-session')
        elif damage == 'poll_input': poll = poll.replace('chars:""', 'chars:"x"')
        elif damage == 'poll_dynamic': poll = 'text(await tools.write_stdin(poll));\n'
        elif damage == 'poll_duplicate': poll = poll.replace('session_id:"other-session"', 'session_id:"other-session",session_id:"third"')
        elif damage == 'poll_nonzero': results[-2]['exit_code'] = 1
        elif damage == 'poll_bool_exit': results[-2]['exit_code'] = False
        elif damage == 'poll_running': results[-2] = {'session_id': 'other-session', 'output': 'waiting'}
        elif damage == 'poll_error': results[-2]['isError'] = True
        elif damage == 'poll_reordered': results[-2:] = reversed(results[-2:])
        prefix = command * (command_count - 1) + poll
        if damage == 'poll_repeated':
            prefix = poll * 2
            results[:] = [{'exit_code': 0, 'output': 'prior poll returned'}] * 2 + [results[-1]]
    rows = args['source'].read_text().splitlines()
    call = json.loads(rows[args['wait_line'] - 1])
    call['payload']['input'] = prefix + wait
    rows[args['wait_line'] - 1] = json.dumps(call)
    output = json.loads(rows[args['wait_line']])
    output['payload']['output'] = [{'type': 'input_text', 'text': 'Script completed'},
        *[{'type': 'input_text', 'text': json.dumps(result)} for result in results]]
    rows[args['wait_line']] = json.dumps(output)
    args['source'].write_text('\n'.join(rows) + '\n')
    original = args['source'].read_bytes()
    cli = ['--operation', 'finish-shutdown', '--owner-task-id', owner, '--run-id', manifest['run_id'],
        '--source-session-file', str(args['source']), '--source-line', str(args['status_line']),
        '--wait-line', str(args['wait_line'])]
    for line in args['interrupt_lines']:
        cli.extend(['--interrupt-line', str(line)])
    if damage in {None, 'too_many_commands'}:
        assert host.main(cli) == 0
        sealed = json.loads(host.registry_file(owner).read_text())['shutdown']
        assert sealed['status'] == 'verified'
        assert sealed['receipt']['driver_exit_code'] == 1
        assert sealed['receipt']['driver_wait_source']['line'] == args['wait_line']
    else:
        with pytest.raises(ValueError): host.main(cli)
        assert json.loads(host.registry_file(owner).read_text())['shutdown']['status'] == 'pending'
    assert args['source'].read_bytes() == original


@pytest.mark.parametrize('damage', [None, 'poll_same_session', 'poll_input', 'poll_dynamic',
    'poll_duplicate', 'poll_nonzero', 'poll_bool_exit', 'poll_running', 'poll_error',
    'poll_reordered', 'poll_repeated', 'missing_result', 'extra_result', 'wrong_exit'])
@pytest.mark.parametrize('command_count', [1, 2])
def test_cli_shutdown_binds_wait_after_prior_terminal_poll(exchange, damage, command_count):
    test_cli_shutdown_binds_wait_after_literal_command(exchange, damage, command_count, prior_poll=True)


@pytest.mark.parametrize('damage', [None, 'extra_wait'])
def test_cli_shutdown_binds_wait_after_delayed_registration(monkeypatch, request, damage):
    from datetime import datetime, timedelta

    class EarlierStart(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - timedelta(seconds=2)

    with monkeypatch.context() as clock:
        clock.setattr(controller_handoff, 'datetime', EarlierStart)
        delayed_exchange = request.getfixturevalue('exchange')
    test_cli_shutdown_binds_wait_after_literal_command(delayed_exchange, damage, 1)


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

@pytest.mark.parametrize('already_closed', [False, True])
def test_abort_after_terminal_wait_does_not_recreate_cycle_stop(exchange, already_closed, monkeypatch):
    _, _, owner, _, _, manifest, _ = exchange
    if already_closed:
        host.close(owner=owner, reason='reviewer_failed')
    controller_handoff.finish(manifest, status='failed', children_reaped=True, health='degraded')
    path = seal_execution(owner, manifest)
    receipt = json.loads(path.read_text())
    receipt.update(result='blocked', exit_code=1, controller_state='failed')
    host.guard.write(path, receipt)
    runtime_control.clear_cycle_stop()
    monkeypatch.setattr(runtime_control, 'controller_active', lambda: False)
    shutdown.begin(owner=owner, reason='reviewer_failed')
    assert not runtime_control.stop_requested()


@pytest.mark.parametrize('active', ['controller_active', 'writer_active', 'active_request_writers'])
def test_abort_after_terminal_wait_still_stops_for_active_runtime(exchange, monkeypatch, active):
    _, _, owner, _, _, manifest, _ = exchange
    controller_handoff.finish(manifest, status='failed', children_reaped=True, health='degraded')
    path = seal_execution(owner, manifest)
    receipt = json.loads(path.read_text())
    receipt.update(result='blocked', exit_code=1, controller_state='failed')
    host.guard.write(path, receipt)
    for name in ('controller_active', 'writer_active', 'active_request_writers'):
        monkeypatch.setattr(runtime_control, name, lambda *args: False)
    monkeypatch.setattr(runtime_control, active, lambda *args: True)
    shutdown.begin(owner=owner, reason='reviewer_failed')
    assert runtime_control.stop_reason() == 'controller_cycle_stop'
