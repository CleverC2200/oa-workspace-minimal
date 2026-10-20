import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from apps.oa_finance_preaudit import approval_lifecycle, host_chat_review as host
from scripts import finance_preaudit_run as runner
from tests.finance_preaudit.test_app_preregistration_shutdown import failed_start as failed_start
from tests.finance_preaudit.test_host_chat_review import native_call as recorded_call


def native_call(parent, *args):
    line = recorded_call(parent, *args)
    rows = parent.read_text().splitlines()
    output = json.loads(rows[line])
    output['timestamp'] = datetime.now(timezone.utc).isoformat()
    rows[line] = json.dumps(output)
    parent.write_text('\n'.join(rows) + '\n')
    return line


@pytest.fixture
def legacy_failure(failed_start):
    args, full = failed_start
    path = args['receipt_file']
    manifest = json.loads(path.with_name('controller-manifest.json').read_text())
    progress_path = approval_lifecycle.run_directory(manifest['run_id']) / 'progress.json'
    progress = json.loads(progress_path.read_text())
    progress['actions'] = [str(runner.PROJECT_ROOT / 'runtime/finance_preaudit_flows/old/action.json')]
    host.guard.write(progress_path, progress)
    output_path = path.with_name('stdout.log')
    output = json.loads(output_path.read_text())
    output.update(peak_active=3, prepare_active_seconds=2.0)
    output['pipeline']['writer']['busy_seconds'] = 0.0
    host.guard.write(output_path, output)
    full['output_sha256']['stdout.log'] = hashlib.sha256(output_path.read_bytes()).hexdigest()
    host.guard.write(path, full)
    wait = native_call(args['source'], 'functions.exec',
        'text(await tools.write_stdin({session_id:"native-session",chars:"",yield_time_ms:1000}));',
        'legacy-terminal', {'exit_code': full['exit_code'],
                            'output': json.dumps({**full, 'receipt_file': str(path)})})
    return {**args, 'source_owner': args['owner'], 'wait_line': wait}, progress_path


def test_legacy_prepared_failure_seals_original_sources_without_reclassifying_actions(legacy_failure):
    args, progress = legacy_failure
    from apps.oa_finance_preaudit import legacy_app_shutdown as legacy
    originals = {p: p.read_bytes() for p in [args['receipt_file'], progress]}
    with pytest.raises(ValueError, match='previous_round_incomplete'):
        host.assert_previous_rounds_closed(owner=str(uuid4()))
    assert legacy.finish(**args, confirm=False)['status'] == 'ready'
    assert not args['receipt_file'].with_name('legacy-app-shutdown.json').exists()
    sealed = legacy.finish(**args, confirm=True)
    assert sealed['status'] == 'verified'
    assert sealed['historical_action_outcomes'] == 'not_verified'
    assert sealed['no_writes_by_original_run'] is True
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    assert legacy.sealed(args['receipt_file']) == sealed
    host.assert_previous_rounds_closed(owner=str(uuid4()))
    assert legacy.finish(**args, confirm=True) == sealed


@pytest.mark.parametrize('damage', ['writer', 'review_started', 'raw_wait_mismatch',
    'missing_interrupt', 'foreign_source', 'changed_progress', 'live_group'])
def test_legacy_shutdown_rejects_untrusted_or_work_bearing_history(legacy_failure, damage, monkeypatch):
    args, progress = legacy_failure
    from apps.oa_finance_preaudit import legacy_app_shutdown as legacy
    original = args['receipt_file'].read_bytes()
    if damage == 'missing_interrupt':
        args['interrupt_lines'] = args['interrupt_lines'][:2]
    elif damage == 'foreign_source':
        args['source_owner'] = str(uuid4())
    elif damage == 'live_group':
        monkeypatch.setattr(host.guard, 'group_alive', lambda _: True)
    elif damage == 'changed_progress':
        legacy.finish(**args, confirm=True)
        data = json.loads(progress.read_text())
        data['actions'].append(str(runner.PROJECT_ROOT / 'runtime/finance_preaudit_flows/new/action.json'))
        host.guard.write(progress, data)
    elif damage == 'raw_wait_mismatch':
        rows = args['source'].read_text().splitlines()
        item = json.loads(rows[args['wait_line']])
        output = json.loads(item['payload']['output'])
        receipt = json.loads(output['output'])
        receipt['owner_task_id'] = str(uuid4())
        output['output'] = json.dumps(receipt)
        item['payload']['output'] = json.dumps(output)
        rows[args['wait_line']] = json.dumps(item)
        args['source'].write_text('\n'.join(rows) + '\n')
    else:
        path = args['receipt_file'].with_name('stdout.log')
        data = json.loads(path.read_text())
        if damage == 'writer':
            data['pipeline']['writer']['max_concurrency'] = 1
        else:
            data['process_attempts'] = 1
        host.guard.write(path, data)
        execution = json.loads(args['receipt_file'].read_text())
        execution['output_sha256']['stdout.log'] = hashlib.sha256(path.read_bytes()).hexdigest()
        host.guard.write(args['receipt_file'], execution)
        args['wait_line'] = native_call(args['source'], 'functions.exec',
            'text(await tools.write_stdin({session_id:"native-session",chars:"",yield_time_ms:1000}));',
            'work-bearing-terminal', {'exit_code': 1,
                                     'output': json.dumps({**execution, 'receipt_file': str(args['receipt_file'])})})
        original = args['receipt_file'].read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        legacy.finish(**args, confirm=True)
    assert args['receipt_file'].read_bytes() == original


def test_legacy_owner_alias_requires_full_original_terminal_payload(legacy_failure):
    args, _ = legacy_failure
    from apps.oa_finance_preaudit import legacy_app_shutdown as legacy
    alias = str(uuid4())
    path = args['receipt_file']
    execution = json.loads(path.read_text())
    execution.update(owner_task_id=alias, received_signals=[15])
    host.guard.write(path, execution)
    binding = json.loads(path.with_name('tool-session.json').read_text())
    binding['owner_task_id'] = alias
    host.guard.write(path.with_name('tool-session.json'), binding)
    manifest = json.loads(path.with_name('controller-manifest.json').read_text())
    manifest['owner_task_id'] = alias
    host.guard.write(path.with_name('controller-manifest.json'), manifest)
    execution['manifest_sha256'] = hashlib.sha256(path.with_name('controller-manifest.json').read_bytes()).hexdigest()
    host.guard.write(path, execution)
    args['owner'] = alias
    args['wait_line'] = native_call(args['source'], 'functions.exec',
        'const r = await tools.write_stdin({session_id:"native-session",chars:"\\u0003",yield_time_ms:1000}); text(JSON.stringify(r));',
        'legacy-interrupted-terminal', {'exit_code': 1, 'output': json.dumps({**execution, 'receipt_file': str(path)})})
    from apps.oa_finance_preaudit import app_review_shutdown
    native, proof = host._native_source(args['source'], args['source_owner'], args['wait_line'], output=True)
    with pytest.raises(ValueError, match='shutdown_wait_untrusted'):
        app_review_shutdown._wait_result(native['anchor'], proof['output'], args['tool_session_id'])
    sealed = legacy.finish(**args, confirm=True)
    assert sealed['owner_task_id'] == alias
    assert sealed['source_owner_task_id'] == args['source_owner']
    assert sealed['historical_owner_matches_native_source'] is False
    host.assert_previous_rounds_closed(owner=str(uuid4()))


def test_public_legacy_sealing_cli_defaults_to_preview(legacy_failure, monkeypatch, capsys):
    args, _ = legacy_failure
    from apps.oa_finance_preaudit import legacy_app_shutdown as legacy
    monkeypatch.setattr(legacy.auth, 'load_local_env', lambda: None)
    command = ['--owner-task-id', args['owner'], '--source-owner-task-id', args['source_owner'],
        '--source-session-file', str(args['source']), '--receipt-file', str(args['receipt_file']),
        '--invocation-id', args['invocation_id'], '--tool-session-id', args['tool_session_id'],
        '--source-line', str(args['status_line']), '--wait-line', str(args['wait_line'])]
    for child, interrupt in zip(args['reviewers'], args['interrupt_lines']):
        command.extend(['--reviewer-session-file', child['session_file'], '--spawn-line', str(child['spawn_line']),
                        '--interrupt-line', str(interrupt)])
    assert legacy.main(command) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'ready'
    assert not args['receipt_file'].with_name('legacy-app-shutdown.json').exists()
    assert legacy.main([*command, '--confirm-seal', 'YES']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'verified'
    host.assert_previous_rounds_closed(owner=str(uuid4()))


@pytest.mark.parametrize('terminal_before_exit', [False, True])
def test_legacy_wait_can_begin_before_exit_when_paired_terminal_returns_after_exit(legacy_failure, terminal_before_exit):
    args, _ = legacy_failure
    from apps.oa_finance_preaudit import legacy_app_shutdown as legacy
    completed = datetime.fromisoformat(json.loads(args['receipt_file'].read_text())['completed_at'])
    rows = args['source'].read_text().splitlines()
    item = json.loads(rows[args['wait_line'] - 1])
    item['timestamp'] = (completed - timedelta(milliseconds=1)).isoformat()
    rows[args['wait_line'] - 1] = json.dumps(item)
    if terminal_before_exit:
        item = json.loads(rows[args['wait_line']])
        item['timestamp'] = (completed - timedelta(milliseconds=1)).isoformat()
        rows[args['wait_line']] = json.dumps(item)
    args['source'].write_text('\n'.join(rows) + '\n')
    if terminal_before_exit:
        with pytest.raises(ValueError, match='stale_source'):
            legacy.finish(**args, confirm=True)
    else:
        assert legacy.finish(**args, confirm=True)['status'] == 'verified'
