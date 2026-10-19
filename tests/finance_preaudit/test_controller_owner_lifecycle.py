import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import approval_guard
from scripts import finance_preaudit_run as runner


def wait_until(predicate, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    pytest.fail('owned execution did not reach the expected state')


def sealed_execution(path):
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    return value if value['state'] == 'exited' and 'result' in value else None


def fake_controller(counter):
    return f'''import json,time
from pathlib import Path
from apps.oa_approval import runtime_control as control
from apps.oa_finance_preaudit import controller_handoff as handoff,approval_lifecycle as lifecycle
with control.global_controller_lease():
    manifest=handoff.begin({{'capacity':3}})
    with lifecycle.run_scope(manifest,Path.cwd()):
        count=0
        while not control.stop_requested():
            lifecycle.heartbeat(manifest['run_id'],manifest['controller_instance_id'],set(),[],writer_active=False)
            if control.stop_requested():break
            count+=1
            Path({str(counter)!r}).write_text(str(count))
            time.sleep(.05)
    time.sleep(.15)
    handoff.finish(manifest,status='failed',children_reaped=True,health='degraded')
print('offline owned controller stopped')
raise SystemExit(7)
'''


@pytest.mark.parametrize('target', ['caller', 'caller_unreaped', 'supervisor'])
def test_forced_owner_exit_stops_and_closes_without_relaunch(tmp_path, monkeypatch, target):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    owner = str(uuid4())
    directory = tmp_path / 'executions' / uuid4().hex
    counter = tmp_path / 'admissions.txt'
    command = [sys.executable, '-c', fake_controller(counter)]
    code = f'''from pathlib import Path
import inspect
from scripts.finance_preaudit_run import supervise
options={{'detached':True}} if 'detached' in inspect.signature(supervise).parameters else {{}}
supervise({command!r},directory=Path({str(directory)!r}),owner_task_id={owner!r},**options)
'''
    caller = subprocess.Popen([sys.executable, '-c', code], cwd=runner.PROJECT_ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
    controller_identity = supervisor_identity = None
    try:
        receipt_path = directory / 'execution.json'
        receipt = wait_until(lambda: json.loads(receipt_path.read_text()) if receipt_path.exists()
                             and json.loads(receipt_path.read_text()).get('state') == 'waiting' else None)
        wait_until(counter.exists)
        controller_identity = approval_guard.process_identity(receipt['controller_pid'])
        supervisor_identity = approval_guard.process_identity(receipt['supervisor_pid'])
        assert controller_identity and supervisor_identity
        os.kill(supervisor_identity['pid'] if target == 'supervisor' else caller.pid, signal.SIGKILL)
        if target != 'supervisor':
            if target == 'caller':
                caller.wait(timeout=2)
            final = wait_until(lambda: sealed_execution(receipt_path))
            assert final['exit_code'] == 7 and final['children_reaped'] is True
            report = runner.read_execution(receipt_path, owner_task_id=owner, invocation_id=directory.name)
            assert report['terminal'] is True and report['status'] == 'blocked'
            assert report['output_files']['stdout.log']['size_bytes'] > 0
        else:
            wait_until(lambda: approval_guard.process_identity(controller_identity['pid']) is None)
            report = runner.read_execution(receipt_path, owner_task_id=owner, invocation_id=directory.name)
            assert report['state'] == 'supervisor_lost'
            assert report['exit_code'] is None and report['terminal'] is False
        assert runtime_control.stop_requested()
        assert not approval_guard.group_alive(controller_identity['pgid'])
        count = counter.read_text()
        time.sleep(.15)
        assert counter.read_text() == count
        assert not runtime_control.writer_active()
    finally:
        runtime_control.request_cycle_stop()
        for identity in (controller_identity, supervisor_identity):
            if identity:
                deadline = time.monotonic() + 3
                while approval_guard.process_identity(identity['pid']) == identity and time.monotonic() < deadline:
                    time.sleep(.05)
        if caller.poll() is None:
            caller.kill()
        caller.wait(timeout=5)


@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGHUP])
def test_detached_caller_signal_waits_for_inflight_writer(tmp_path, monkeypatch, signum):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    monkeypatch.setenv('OA_FROZEN_WRITER_LOCK_FILE', str(tmp_path / 'writer.lock'))
    writer_lock = runtime_control.request_writer_lock_file(runtime_control.GLOBAL_WRITER_KEY)
    started, finished = tmp_path / 'writer-started', tmp_path / 'writer-finished'
    command = [sys.executable, '-c', f'''import time
from pathlib import Path
from apps.oa_approval import runtime_control as control
from apps.oa_finance_preaudit import controller_handoff as handoff,approval_lifecycle as lifecycle
with control.global_controller_lease():
    manifest=handoff.begin({{'capacity':3}})
    with lifecycle.run_scope(manifest,Path.cwd()):
        with control.global_writer_lease():
            Path({str(started)!r}).touch()
            while not control.stop_requested():
                lifecycle.heartbeat(manifest['run_id'],manifest['controller_instance_id'],set(),[],writer_active=True)
                time.sleep(.05)
            time.sleep(.4)
            Path({str(finished)!r}).touch()
    handoff.finish(manifest,status='failed',children_reaped=True,health='degraded')
print('offline writer finished')
raise SystemExit(7)
''']
    owner = str(uuid4())
    directory = tmp_path / 'executions' / uuid4().hex
    code = f'''from pathlib import Path
from scripts.finance_preaudit_run import supervise
supervise({command!r},directory=Path({str(directory)!r}),owner_task_id={owner!r},detached=True)
'''
    caller = subprocess.Popen([sys.executable, '-c', code], cwd=runner.PROJECT_ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        wait_until(started.exists)
        assert runtime_control.writer_active(writer_lock)
        caller.send_signal(signum)
        wait_until(runtime_control.stop_requested)
        assert not finished.exists() and caller.poll() is None
        assert runtime_control.writer_active(writer_lock)
        assert caller.wait(timeout=6) == 0
        assert finished.exists() and not runtime_control.writer_active(writer_lock)
        report = runner.read_execution(directory / 'execution.json', owner_task_id=owner, invocation_id=directory.name)
        assert report['state'] == 'exited' and report['exit_code'] == 7
        assert report['terminal'] is True and report['children_reaped'] is True
        assert report['output_files']['stdout.log']['size_bytes'] > 0
    finally:
        runtime_control.request_cycle_stop()
        caller.wait(timeout=6)


def test_normal_tool_parent_exit_stops_detached_chain(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    owner = str(uuid4())
    directory = tmp_path / 'executions' / uuid4().hex
    counter = tmp_path / 'admissions.txt'
    command = [sys.executable, '-c', fake_controller(counter)]
    caller_code = f'''from pathlib import Path
from scripts.finance_preaudit_run import supervise
supervise({command!r},directory=Path({str(directory)!r}),owner_task_id={owner!r},detached=True)
'''
    parent_code = f'''import subprocess,sys,time
from pathlib import Path
subprocess.Popen([sys.executable,'-c',{caller_code!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
while not Path({str(counter)!r}).exists():time.sleep(.05)
'''
    tool = subprocess.Popen([sys.executable, '-c', parent_code], cwd=runner.PROJECT_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        assert tool.wait(timeout=6) == 0
        receipt_path = directory / 'execution.json'
        final = wait_until(lambda: sealed_execution(receipt_path))
        assert final['owner_process_lost'] is True and final['exit_code'] == 7
        report = runner.read_execution(receipt_path, owner_task_id=owner, invocation_id=directory.name)
        assert report['terminal'] is True and report['children_reaped'] is True
        assert runtime_control.stop_requested()
    finally:
        runtime_control.request_cycle_stop()
        tool.wait(timeout=6)


def test_exited_wait_ignores_unsealed_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    owner = str(uuid4())
    directory = tmp_path / 'executions' / uuid4().hex
    receipt_path = directory / 'execution.json'
    manifest_path = directory / 'controller-manifest.json'
    read = runtime_control.read_control_bytes
    observed = []

    def observe_before_manifest_attestation(path):
        if Path(path) == manifest_path and not observed:
            observed.append(sealed_execution(receipt_path))
        return read(path)

    monkeypatch.setattr(runtime_control, 'read_control_bytes', observe_before_manifest_attestation)
    command = [sys.executable, '-c', '''import time
from apps.oa_approval import runtime_control as control
from apps.oa_finance_preaudit import controller_handoff as handoff
with control.global_controller_lease():
    manifest=handoff.begin({'capacity':3})
    handoff.finish(manifest,status='failed',children_reaped=True,health='degraded')
time.sleep(.15)
raise SystemExit(7)
''']
    runner.supervise(command, directory=directory, owner_task_id=owner)
    assert observed == [None]
    final = sealed_execution(receipt_path)
    assert final['exit_code'] == 7 and final['children_reaped'] is True
    report = runner.read_execution(receipt_path, owner_task_id=owner, invocation_id=directory.name)
    assert report['terminal'] is True and report['status'] == 'blocked'


@pytest.mark.parametrize('error_code', ['controller_execution_snapshot_changed', 'controller_execution_evidence_failed'])
def test_detached_snapshot_race_does_not_cancel_healthy_controller(tmp_path, monkeypatch, error_code):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    read = runner._read_evidence
    interrupted = []
    started = tmp_path / 'controller-started'

    def execution_diagnostics():
        fields = ('state', 'exit_code', 'exit_signal', 'error_type', 'received_signals',
                  'children_reaped', 'stop_wait_error', 'stop_request_error', 'owner_process_lost')
        return json.dumps({**{key: receipt.get(key) for key in fields}, 'started': started.exists()},
                          sort_keys=True)

    def raced_read(path):
        if path.name == 'execution.json' and not interrupted:
            wait_until(started.exists)
            interrupted.append(True)
            raise ValueError(error_code)
        return read(path)

    monkeypatch.setattr(runner, '_read_evidence', raced_read)
    directory = tmp_path / 'executions' / uuid4().hex
    options = dict(directory=directory, owner_task_id=str(uuid4()), detached=True)
    command = [sys.executable, '-c', f"import time\nfrom pathlib import Path\nPath({str(started)!r}).touch()\ntime.sleep(.4)"]
    if error_code == 'controller_execution_snapshot_changed':
        receipt = runner.supervise(command, **options)
        assert receipt['exit_code'] == 0 and not receipt['received_signals'], execution_diagnostics()
        assert not runtime_control.stop_requested(), execution_diagnostics()
    else:
        with pytest.raises(ValueError, match=error_code):
            runner.supervise(command, **options)
        receipt = json.loads((directory / 'execution.json').read_text())
        assert receipt['state'] == 'exited' and receipt['exit_code'] == 0, execution_diagnostics()
        assert receipt['received_signals'] == [signal.SIGTERM], execution_diagnostics()
    assert interrupted, execution_diagnostics()


def test_lost_legacy_supervisor_is_not_projected_as_waiting(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    owner = str(uuid4())
    directory = tmp_path / 'executions' / uuid4().hex
    receipt = runner.supervise([sys.executable, '-c', 'pass'], directory=directory, owner_task_id=owner)
    path = Path(receipt['receipt_file'])
    receipt.update(state='waiting', supervisor_pid=2147483647, exit_code=None, exit_signal=None)
    receipt.pop('supervisor_process', None)
    runtime_control.write_control_bytes_atomic(path, json.dumps(receipt).encode())
    original = path.read_bytes()
    report = runner.read_execution(path, owner_task_id=owner, invocation_id=directory.name)
    assert report['state'] == 'supervisor_lost' and report['recorded_state'] == 'waiting'
    assert report['status'] == 'blocked' and report['terminal'] is False
    assert report['exit_code'] is None and path.read_bytes() == original


def test_detached_waiter_keeps_silent_healthy_execution_and_actual_result(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    code = '''import time
from apps.oa_approval import runtime_control as control
from apps.oa_finance_preaudit import controller_handoff as handoff
with control.global_controller_lease():
    manifest=handoff.begin({'capacity':3})
    time.sleep(.3)
    handoff.finish(manifest,status='completed',children_reaped=True,health='healthy')
'''
    owner = str(uuid4())
    directory = tmp_path / 'executions' / uuid4().hex
    receipt = runner.supervise([sys.executable, '-c', code], directory=directory, owner_task_id=owner, detached=True)
    assert receipt['result'] == 'completed' and receipt['exit_code'] == 0
    assert not receipt.get('owner_process_lost') and not runtime_control.stop_requested()
    report = runner.read_execution(directory / 'execution.json', owner_task_id=owner, invocation_id=directory.name)
    assert report['status'] == 'completed' and report['terminal'] is True
    assert not (directory / 'supervisor-exit.json').exists()


def test_pid_reused_supervisor_is_blocked_without_changing_original_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    owner = str(uuid4())
    directory = tmp_path / 'executions' / uuid4().hex
    receipt = runner.supervise([sys.executable, '-c', 'pass'], directory=directory, owner_task_id=owner)
    receipt.update(state='waiting', exit_code=None, exit_signal=None)
    path = directory / 'execution.json'
    runtime_control.write_control_bytes_atomic(path, json.dumps(receipt).encode())
    saved = path.read_bytes()
    identity = receipt['supervisor_process']
    monkeypatch.setattr(approval_guard, 'process_identity', lambda pid: {**identity, 'birth': 'different'})
    report = runner.read_execution(path, owner_task_id=owner, invocation_id=directory.name)
    assert report['state'] == 'supervisor_lost' and report['status'] == 'blocked'
    assert report['exit_code'] is None and path.read_bytes() == saved


def lost_supervisor(tmp_path, monkeypatch, returncode):
    controls = {key: value for key, value in os.environ.items() if key in {
        'OA_FINANCE_PREAUDIT_STOP_FILE', 'OA_FROZEN_WRITER_LOCK_FILE',
        'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE'}}
    for key in list(os.environ):
        if key.startswith('OA_') or key in {'PYTHONPATH', 'PYTHONHOME'}:
            monkeypatch.delenv(key)
    for key, value in {**controls, 'HOME': str(tmp_path / 'home'),
                       'CODEX_HOME': str(tmp_path / 'home/codex'),
                       'OA_REVIEW_PROVIDER': 'openai', 'OA_REVIEW_TRANSPORT': 'host_chat'}.items():
        monkeypatch.setenv(key, value)
    guard = '''import socket
def reject_network(*args, **kwargs): raise RuntimeError('offline_network_forbidden')
socket.getaddrinfo=reject_network
socket.socket.connect=reject_network
socket.create_connection=reject_network
'''
    real_popen = subprocess.Popen
    entry = 'from scripts.finance_preaudit_run import _supervisor_entry; _supervisor_entry()'

    def launch(command, *args, **kwargs):
        if command == [sys.executable, '-c', entry]:
            handler = f'import os,signal\nsignal.signal(signal.SIGUSR1,lambda *_: os._exit({returncode}))\n'
            command = [sys.executable, '-c', guard + handler + entry]
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', launch)
    owner = str(uuid4())
    directory = tmp_path / 'control/executions' / uuid4().hex
    before = tmp_path / 'before.json'
    signum = signal.SIGKILL if returncode < 0 else signal.SIGUSR1
    child = guard + f'''import os,json,time,signal
from pathlib import Path
path=Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE']).with_name('execution.json')
deadline=time.monotonic()+5
while time.monotonic()<deadline:
    value=json.loads(path.read_text())
    if value['state']=='waiting' and value['controller_pid']==os.getpid():break
    time.sleep(.01)
else:raise RuntimeError('synthetic_controller_not_waiting')
assert value['supervisor_pid']==os.getppid()
Path({str(before)!r}).write_bytes(path.read_bytes())
os.kill(os.getppid(),{int(signum)})
os._exit(0)
'''
    with pytest.raises(ValueError, match='^controller_supervisor_lost$'):
        runner.supervise([sys.executable, '-c', child], directory=directory,
                         owner_task_id=owner, detached=True)
    saved = before.read_bytes()
    receipt = json.loads(saved)
    wait_until(lambda: approval_guard.process_identity(receipt['controller_pid']) is None)
    assert (directory / 'execution.json').read_bytes() == saved
    return owner, directory, receipt


@pytest.mark.parametrize('returncode,signum', [(0, None), (7, None), (-signal.SIGKILL, signal.SIGKILL)])
def test_lost_supervisor_retains_actual_wait_observation_without_claiming_completion(tmp_path, monkeypatch, returncode, signum):
    owner, directory, receipt = lost_supervisor(tmp_path, monkeypatch, returncode)
    report = runner.read_execution(directory / 'execution.json', owner_task_id=owner,
                                   invocation_id=directory.name)
    observation = report['supervisor_exit_observation']
    assert observation['wait_returncode'] == returncode and observation['wait_signal'] == signum
    assert observation['supervisor_pid'] == receipt['supervisor_pid']
    assert observation['owner_task_id'] == owner and observation['invocation_id'] == directory.name
    assert report['state'] == 'supervisor_lost' and report['status'] == 'blocked'
    assert report['terminal'] is False and report['exit_code'] is None
    assert report['children_reaped'] == 'unknown'
    assert (directory / 'supervisor-exit.json').stat().st_mode & 0o777 == 0o600


def test_missing_or_invalid_supervisor_observation_cannot_complete_lost_execution(tmp_path, monkeypatch):
    owner, directory, _ = lost_supervisor(tmp_path, monkeypatch, 0)
    path = directory / 'execution.json'
    saved = path.read_bytes()
    diagnostic = directory / 'supervisor-exit.json'
    observation = json.loads(diagnostic.read_bytes())
    diagnostic.unlink()
    invalid = [None, b'{', *[json.dumps({**observation, key: value}).encode() for key, value in (
        ('owner_task_id', str(uuid4())), ('invocation_id', uuid4().hex),
        ('supervisor_pid', observation['supervisor_pid'] + 1), ('wait_signal', signal.SIGTERM),
        ('wait_returncode', True), ('observed_at', '2000-01-01T00:00:00+00:00'))]]
    for content in invalid:
        if content is not None:
            diagnostic.write_bytes(content)
        report = runner.read_execution(path, owner_task_id=owner, invocation_id=directory.name)
        assert report['status'] == 'blocked' and report['terminal'] is False
        assert report['exit_code'] is None and report['children_reaped'] == 'unknown'
        assert 'supervisor_exit_observation' not in report
        assert path.read_bytes() == saved
    diagnostic.unlink()
    diagnostic.symlink_to(path)
    assert 'supervisor_exit_observation' not in runner.read_execution(
        path, owner_task_id=owner, invocation_id=directory.name)


def test_supervisor_observation_write_failure_preserves_original_loss(tmp_path, monkeypatch):
    real_open = os.open

    def unwritable(path, flags, *args, **kwargs):
        if path == 'supervisor-exit.json' and flags & os.O_CREAT:
            raise PermissionError('synthetic_diagnostic_unwritable')
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', unwritable)
    owner, directory, _ = lost_supervisor(tmp_path, monkeypatch, 7)
    report = runner.read_execution(directory / 'execution.json', owner_task_id=owner,
                                   invocation_id=directory.name)
    assert report['status'] == 'blocked' and report['error_code'] == 'controller_supervisor_lost'
    assert report['terminal'] is False and report['exit_code'] is None
    assert not (directory / 'supervisor-exit.json').exists()


@pytest.mark.parametrize("failure_code", ["controller_execution_snapshot_changed", "token_secret_identifier"])
def test_supervisor_callback_failure_keeps_original_error_and_terminal_evidence(tmp_path, monkeypatch, failure_code):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    owner = str(uuid4())
    directory = tmp_path / 'executions' / uuid4().hex
    identity = approval_guard.process_identity(os.getpid())
    original_identity = approval_guard.process_identity
    calls = []

    def owner_identity(pid):
        receipt_file = directory / 'execution.json'
        if pid == identity['pid'] and receipt_file.exists() and json.loads(receipt_file.read_text())['state'] == 'waiting':
            return None
        return original_identity(pid)

    monkeypatch.setattr(approval_guard, 'process_identity', owner_identity)

    def controller_owner():
        calls.append(True)
        if len(calls) == 1:
            raise ValueError(failure_code)
        return json.loads((directory / 'execution.json').read_text())['controller_pid']

    def stop_failure():
        raise RuntimeError('controller_stop_request_failed')

    monkeypatch.setattr(runtime_control, 'controller_owner_pid', controller_owner)
    monkeypatch.setattr(runtime_control, 'request_cycle_stop', stop_failure)
    command = [sys.executable, '-c', '''import time
from apps.oa_approval import runtime_control as control
from apps.oa_finance_preaudit import controller_handoff as handoff
with control.global_controller_lease():
    manifest=handoff.begin({'capacity':3})
    time.sleep(.15)
    handoff.finish(manifest,status='failed',children_reaped=True,health='degraded')
print('offline callback failure')
raise SystemExit(7)
''']
    with pytest.raises(ValueError, match=failure_code):
        runner.supervise(command, directory=directory, owner_task_id=owner, owners=[identity])
    receipt = json.loads((directory / 'execution.json').read_text())
    assert receipt['primary_failure'] == {'error_type': 'ValueError', 'error_code': failure_code if failure_code.startswith('controller_') else 'controller_supervisor_failure',
                                           'phase': 'controller_wait'}
    assert receipt['cleanup_failures'] == [{'error_type': 'RuntimeError', 'error_code': 'controller_stop_request_failed',
                                            'phase': 'stop_request'}]
    report = runner.read_execution(directory / 'execution.json', owner_task_id=owner, invocation_id=directory.name)
    assert report['terminal'] is True and report['status'] == 'blocked'
    assert report['exit_code'] == 7 and report['children_reaped'] is True
    assert report['primary_failure'] == receipt['primary_failure']
    assert report['output_files']['stdout.log']['size_bytes'] > 0


def test_terminal_log_limit_failure_keeps_actual_wait_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    monkeypatch.setattr(runner, 'LOG_LIMIT', 4)
    owner = str(uuid4())
    directory = tmp_path / 'executions' / uuid4().hex
    command = [sys.executable, '-c', '''from apps.oa_approval import runtime_control as control
from apps.oa_finance_preaudit import controller_handoff as handoff
with control.global_controller_lease():
    manifest=handoff.begin({'capacity':3})
    handoff.finish(manifest,status='failed',children_reaped=True,health='degraded')
print('larger than the bound')
raise SystemExit(7)
''']
    with pytest.raises(ValueError, match='controller_execution_output_limit'):
        runner.supervise(command, directory=directory, owner_task_id=owner)
    receipt = json.loads((directory / 'execution.json').read_text())
    assert receipt['exit_code'] == 7 and receipt['children_reaped'] is True
    assert receipt['primary_failure'] == {'error_type': 'ValueError', 'error_code': 'controller_execution_output_limit',
                                           'phase': 'terminal_binding'}
    assert receipt['cleanup_failures'][0]['error_code'] == 'controller_execution_output_limit'
    assert receipt['manifest_sha256'] and 'result' not in receipt
    report = runner.read_execution(directory / 'execution.json', owner_task_id=owner, invocation_id=directory.name, retrieve=True)
    assert report['terminal'] is False and report['status'] == 'blocked'
