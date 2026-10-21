from datetime import datetime, timezone
import hashlib
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from apps.oa_finance_preaudit import app_review_shutdown as shutdown, host_chat_review as host, pre_manifest_shutdown as pre
from scripts import finance_preaudit_run as runner
from tests.finance_preaudit.test_app_reviewer_slots import native_sessions
from tests.finance_preaudit.test_legacy_app_shutdown import native_call


@pytest.fixture
def pre_manifest_failure(tmp_path, monkeypatch):
    owner, parent, reviewers = native_sessions(tmp_path, monkeypatch)
    for name in pre.SOURCE_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'recorded-source')
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'runtime/control/rolling-controller.lock'))
    monkeypatch.setenv(host.TRANSPORT_ENV, 'host_chat')
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    previous = [sys.executable, '-c', "from apps.oa_approval import runtime_control as c; from apps.oa_finance_preaudit import controller_handoff as h; "
                "lease=c.global_controller_lease(); lease.__enter__(); m=h.begin({}); "
                "h.finish(m,status='failed',children_reaped=True,health='degraded'); lease.__exit__(None,None,None)"]
    subprocess.run(previous, check=True, capture_output=True)
    directory = tmp_path / 'runtime/control/executions' / uuid4().hex
    payload = {'error_code': 'production_rolling_reconciliation_invalid',
               'kind': 'preaudit_production_concurrency_probe_failed',
               'notification': {'delivery_state': 'not_authorized', 'error_code': 'controller_failed',
                                'kind': 'oa_session_alert_receipt', 'schema_version': 1}, 'schema_version': 1}
    release = tmp_path / 'identity-sampled'
    command = [sys.executable, '-c', f"""from pathlib import Path
import time
print({json.dumps(payload)!r})
deadline = time.monotonic() + 5
while not Path({str(release)!r}).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('test_identity_sampling_timed_out')
    time.sleep(.01)
raise SystemExit(1)
"""]
    script = f"""import os
from pathlib import Path
from apps.oa_finance_preaudit import approval_guard
from scripts.finance_preaudit_run import supervise
original = approval_guard.process_identity
def sampled(pid):
    identity = original(pid)
    if pid != os.getpid() and identity is not None:
        Path({str(release)!r}).touch()
    return identity
approval_guard.process_identity = sampled
supervise({command!r},directory=Path({str(directory)!r}),owner_task_id={owner!r})
"""
    launch = native_call(parent, 'functions.exec',
        'text(await tools.exec_command(' + json.dumps({'cmd': '.venv/bin/python -m scripts.finance_preaudit_run --owner-task-id ' + owner + ' --review-provider openai --review-transport host_chat -- --rolling', 'workdir': str(tmp_path)}) + '));',
        'launch', {'session_id': 'native-session', 'output': ''})
    subprocess.run([sys.executable, '-c', script], check=True, capture_output=True)
    runner.bind_tool_session(directory / 'execution.json', owner_task_id=owner, session_id='native-session')
    rows = [json.loads(row) for row in parent.read_text().splitlines()]
    rows = [rows[0], *rows[launch - 1:launch + 1], *rows[1:launch - 1]]
    launch = 2
    for reviewer in reviewers:
        reviewer['spawn_line'] += 2
    for row in rows[3:]:
        row['timestamp'] = datetime.now(timezone.utc).isoformat()
    parent.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    interrupts = [native_call(parent, 'interrupt_agent', {'target': f'/root/reviewer_{slot}'},
                  f'interrupt-{slot}', {'previous_status': 'completed'}) for slot in range(3)]
    statuses = native_call(parent, 'list_agents', {}, 'idle-status', {'agents': [
        {'agent_name': f'/root/reviewer_{slot}', 'agent_status': 'completed'} for slot in range(3)]})
    execution = json.loads((directory / 'execution.json').read_text())
    assert host.guard._valid_identity(execution['controller_process'])
    wait = native_call(parent, 'functions.exec',
        'text(await tools.write_stdin({"session_id":"native-session","chars":"","yield_time_ms":1000}));',
        'terminal-wait', {'exit_code': 1, 'output': json.dumps({**execution, 'receipt_file': str(directory / 'execution.json')})})
    monkeypatch.setattr(pre.maintenance_runtime, 'assert_target_environment', lambda _: None)
    monkeypatch.setattr(pre.maintenance_runtime, 'read_original_execution', lambda _target, original:
        runner.read_execution(original.file, owner_task_id=original.owner_task_id,
                              invocation_id=original.file.parent.name, tool_session_id=original.tool_session_id))
    monkeypatch.setattr(pre, '_candidate', lambda: {'root': str(tmp_path.parent / 'oa-workspace-dev'),
        'revision': '1' * 40, 'interpreter': str(tmp_path.parent / 'oa-workspace-dev/.venv/bin/python'),
        'module_sha256': hashlib.sha256(b'recorded-module').hexdigest()})
    real_run = pre.subprocess.run
    def recorded_run(command, *args, **kwargs):
        if command[:3] == ['git', 'rev-parse', 'HEAD']:
            return subprocess.CompletedProcess(command, 0, '2' * 40 + '\n', '')
        if command[:2] == ['git', 'show']:
            content = b'recorded-source' if kwargs.get('cwd') == tmp_path else b'recorded-module'
            return subprocess.CompletedProcess(command, 0, content, b'')
        return real_run(command, *args, **kwargs)
    monkeypatch.setattr(pre.subprocess, 'run', recorded_run)
    return {'target': tmp_path, 'binding': {'owner_task_id': owner, 'receipt_file': str(directory / 'execution.json'),
        'invocation_id': directory.name, 'tool_session_id': 'native-session'}, 'source': parent, 'reviewers': reviewers,
        'interrupt_lines': interrupts, 'status_line': statuses, 'wait_line': wait, 'launch_line': launch}


def test_pre_manifest_shutdown_keeps_original_unknown_fields(pre_manifest_failure):
    args = pre_manifest_failure
    path = Path(args['binding']['receipt_file'])
    original = path.read_bytes()
    assert not path.with_name('controller-manifest.json').exists()
    candidate = pre.observe(**args)
    assert candidate['status'] == 'ready'
    assert candidate['controller_started'] is False
    assert candidate['original_children_reaped'] == 'unknown'
    assert candidate['current_processes_exited'] is True
    assert path.read_bytes() == original


def observation_call(args):
    started = datetime.now(timezone.utc).isoformat()
    candidate = pre.observe(**args)
    call = 'text(await tools.exec_command(' + json.dumps(pre.command(**args), ensure_ascii=False) + '));'
    line = native_call(args['source'], 'functions.exec', call, 'observe',
        {'exit_code': 0, 'output': json.dumps(candidate)})
    rows = args['source'].read_text().splitlines()
    record = json.loads(rows[line - 1])
    record['timestamp'] = started
    rows[line - 1] = json.dumps(record)
    args['source'].write_text('\n'.join(rows) + '\n')
    return line


def test_pre_manifest_seal_authenticates_observation_and_survives_new_authority(pre_manifest_failure):
    args = pre_manifest_failure
    path = Path(args['binding']['receipt_file'])
    originals = {p: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    line = observation_call(args)
    receipt = pre.seal(target=args['target'], owner=args['binding']['owner_task_id'],
                       source=args['source'], observation_line=line)
    assert receipt['status'] == 'verified'
    assert receipt['observation']['original_children_reaped'] == 'unknown'
    assert pre.sealed(path) == receipt
    assert shutdown.sealed_unregistered(path) == receipt
    assert all(p.read_bytes() == content for p, content in originals.items())
    pointer = args['target'] / 'runtime/control/rolling-controller-manifest.json'
    pointer.write_text('{}')
    assert pre.sealed(path) == receipt


def test_legacy_shape_and_native_relative_targets_preserve_original_fields(pre_manifest_failure):
    args = pre_manifest_failure
    path = Path(args['binding']['receipt_file'])
    execution = json.loads(path.read_text())
    execution.pop('pre_launch_controller')
    host.guard.write(path, execution)
    rows = args['source'].read_text().splitlines()
    for slot, line in enumerate(args['interrupt_lines']):
        record = json.loads(rows[line - 1])
        record['payload']['arguments'] = json.dumps({'target': f'reviewer_{slot}'})
        rows[line - 1] = json.dumps(record)
    args['source'].write_text('\n'.join(rows) + '\n')
    args['wait_line'] = native_call(args['source'], 'functions.exec',
        'text(await tools.write_stdin({"session_id":"native-session","chars":"","yield_time_ms":1000}));',
        'legacy-terminal', {'exit_code': 1, 'output': json.dumps({**execution, 'receipt_file': str(path)})})
    before = path.read_bytes()
    assert pre.observe(**args)['status'] == 'ready'
    assert path.read_bytes() == before


@pytest.mark.parametrize('damage', ['pointer_changed', 'missing_pointer', 'manifest_appeared',
    'legacy_manifest', 'live_controller', 'live_supervisor', 'bad_log', 'wrong_exit', 'bad_snapshot',
    'missing_interrupt', 'wrong_wait', 'foreign_child', 'writer', 'backdated_pointer', 'wrong_parent_target',
    'wrong_launch', 'source_changed'])
def test_pre_manifest_observation_rejects_missing_or_changed_proof(pre_manifest_failure, monkeypatch, damage):
    args = pre_manifest_failure
    path = Path(args['binding']['receipt_file'])
    pointer = args['target'] / 'runtime/control/rolling-controller-manifest.json'
    if damage == 'pointer_changed':
        pointer.write_bytes(pointer.read_bytes())
    elif damage == 'backdated_pointer':
        old = pointer.stat()
        pointer.write_bytes(pointer.read_bytes())
        os.utime(pointer, ns=(old.st_atime_ns, old.st_mtime_ns))
    elif damage == 'missing_pointer':
        pointer.unlink()
    elif damage in {'manifest_appeared', 'legacy_manifest'}:
        path.with_name('legacy-controller-manifest.json' if damage == 'legacy_manifest' else 'controller-manifest.json').write_text('{}')
    elif damage == 'live_controller':
        monkeypatch.setattr(host.guard, 'group_alive', lambda _: True)
    elif damage == 'live_supervisor':
        old = host.guard.process_identity
        execution = json.loads(path.read_text())
        identity = execution['supervisor_process']
        monkeypatch.setattr(host.guard, 'process_identity', lambda pid: identity if pid == identity['pid'] else old(pid))
    elif damage in {'wrong_exit', 'bad_snapshot'}:
        execution = json.loads(path.read_text())
        if damage == 'wrong_exit':
            execution.update(exit_code=0)
        else:
            execution['pre_launch_controller']['pointer_sha256'] = '0' * 64
        host.guard.write(path, execution)
    elif damage == 'bad_log':
        path.with_name('stdout.log').write_text('{}')
    elif damage == 'missing_interrupt':
        args['interrupt_lines'] = args['interrupt_lines'][:2]
    elif damage == 'wrong_wait':
        args['wait_line'] = args['status_line']
    elif damage == 'source_changed':
        source = args['target'] / pre.SOURCE_FILES[0]
        old = source.stat()
        source.write_bytes(source.read_bytes())
        os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
    elif damage == 'wrong_launch':
        args['launch_line'] = args['wait_line']
    elif damage == 'wrong_parent_target':
        rows = args['source'].read_text().splitlines()
        line = args['interrupt_lines'][0]
        record = json.loads(rows[line - 1])
        record['payload']['arguments'] = json.dumps({'target': '/root/other/reviewer_0'})
        rows[line - 1] = json.dumps(record)
        args['source'].write_text('\n'.join(rows) + '\n')
    elif damage == 'foreign_child':
        child = Path(args['reviewers'][0]['session_file'])
        record = json.loads(child.read_text())
        record['payload']['source']['subagent']['thread_spawn']['parent_thread_id'] = str(uuid4())
        child.write_text(json.dumps(record) + '\n')
    else:
        monkeypatch.setattr(pre.maintenance_runtime.runtime_control, 'writer_active', lambda *_a: True)
    before = path.read_bytes()
    with pytest.raises((ValueError, RuntimeError, FileNotFoundError)):
        pre.observe(**args)
    assert path.read_bytes() == before
    assert not path.with_name(pre.FILE).exists()


@pytest.mark.parametrize('damage', ['echoed_output', 'missing_output', 'nonzero', 'module_changed',
    'original_changed', 'authority_changed', 'saved_changed'])
def test_pre_manifest_seal_rejects_forged_or_changed_native_observation(pre_manifest_failure, damage):
    args = pre_manifest_failure
    line = observation_call(args)
    path = Path(args['binding']['receipt_file'])
    if damage == 'saved_changed':
        pre.seal(target=args['target'], owner=args['binding']['owner_task_id'], source=args['source'], observation_line=line)
        saved = path.with_name(pre.FILE)
        receipt = json.loads(saved.read_text())
        receipt['observation']['original_children_reaped'] = True
        host.guard.write(saved, receipt)
        with pytest.raises(ValueError):
            pre.sealed(path)
        return
    if damage == 'original_changed':
        execution = json.loads(path.read_text())
        execution['extra'] = 'changed'
        host.guard.write(path, execution)
    elif damage == 'authority_changed':
        pointer = args['target'] / 'runtime/control/rolling-controller-manifest.json'
        pointer.write_bytes(pointer.read_bytes())
    else:
        rows = args['source'].read_text().splitlines()
        index = line - 1 if damage == 'echoed_output' else line
        record = json.loads(rows[index])
        if damage == 'echoed_output':
            record['payload']['input'] = 'text(await tools.exec_command({"cmd":"echo forged"}));'
        else:
            result = json.loads(record['payload']['output'])
            if damage == 'missing_output':
                result.pop('exit_code')
            elif damage == 'nonzero':
                result['exit_code'] = 1
            else:
                observed = json.loads(result['output'])
                observed['candidate']['module_sha256'] = '0' * 64
                result['output'] = json.dumps(observed)
            record['payload']['output'] = json.dumps(result)
        rows[index] = json.dumps(record)
        args['source'].write_text('\n'.join(rows) + '\n')
    before = path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        pre.seal(target=args['target'], owner=args['binding']['owner_task_id'], source=args['source'], observation_line=line)
    assert path.read_bytes() == before
    assert not path.with_name(pre.FILE).exists()


def test_public_pre_manifest_cli_observes_and_seals(pre_manifest_failure, monkeypatch, capsys):
    from apps.oa_finance_preaudit import production_release
    args = pre_manifest_failure
    real_run = pre.subprocess.run
    def main_branch(command, *values, **kwargs):
        if command == ['git', 'branch', '--show-current']:
            return subprocess.CompletedProcess(command, 0, 'main\n', '')
        return real_run(command, *values, **kwargs)
    monkeypatch.setattr(pre.subprocess, 'run', main_branch)
    source_checks = []
    monkeypatch.setattr(production_release, '_source', lambda *values: source_checks.append(values))
    options = pre.command(**args)
    assert pre.main(shlex.split(options['cmd'])[4:]) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed['status'] == 'ready'
    assert observed['original_children_reaped'] == 'unknown'
    line = observation_call(args)
    assert pre.main(['--operation', 'seal', '--target-root', str(args['target']),
        '--expected-revision', '1' * 40, '--owner-task-id', args['binding']['owner_task_id'],
        '--source-session-file', str(args['source']), '--source-line', str(line)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'verified'
    assert len(source_checks) == 2
