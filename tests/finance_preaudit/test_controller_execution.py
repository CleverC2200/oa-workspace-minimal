import json
import os
from pathlib import Path
import signal
import sys
from uuid import uuid4

import pytest

from scripts.finance_preaudit_run import supervise


def run(tmp_path, code):
    return supervise([sys.executable, "-c", code], directory=tmp_path / uuid4().hex,
                     owner_task_id=str(uuid4()))


@pytest.mark.parametrize('code,exit_code,exit_signal', [
    ('import time; time.sleep(.1)', 0, None),
    ('raise SystemExit(7)', 7, None),
    ('import os,signal; os.kill(os.getpid(),signal.SIGTERM)', -signal.SIGTERM, signal.SIGTERM),
])
def test_real_wait_result_does_not_claim_cleanup(tmp_path, code, exit_code, exit_signal):
    receipt = run(tmp_path, code)
    assert receipt['state'] == 'exited'
    assert receipt['exit_code'] == exit_code
    assert receipt['exit_signal'] == exit_signal
    assert receipt['children_reaped'] == 'unknown'
    assert receipt['result'] == 'blocked'
    assert receipt['tool_session_id'] == 'unknown'
    assert Path(receipt['receipt_file']).stat().st_mode & 0o777 == 0o600


def test_current_child_manifest_proves_completion(tmp_path):
    code = '''import os,json,datetime
from pathlib import Path
Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE']).write_text(json.dumps({
 'kind':'preaudit_controller_run_manifest','controller_pid':os.getpid(),
 'run_id':'a'*32,'controller_instance_id':'b'*32,'state':'completed',
 'children_reaped':True,'code_revision':'c'*40,
 'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}))
print('real output')'''
    receipt = run(tmp_path, code)
    assert receipt['result'] == 'completed'
    assert receipt['run_id'] == 'a' * 32
    assert Path(receipt['receipt_file']).with_name('stdout.log').read_text() == 'real output\n'


def test_old_or_wrong_owner_manifest_is_unknown(tmp_path):
    code = '''import os,json
from pathlib import Path
Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE']).write_text(json.dumps({
 'kind':'preaudit_controller_run_manifest','controller_pid':os.getpid()+1,
 'run_id':'a'*32,'controller_instance_id':'b'*32,'state':'completed',
 'children_reaped':True,'started_at':'2020-01-01T00:00:00+00:00'}))'''
    assert run(tmp_path, code)['controller_state'] == 'unknown'


def test_start_failure_is_durable_and_directory_cannot_be_reused(tmp_path):
    directory = tmp_path / uuid4().hex
    receipt = supervise(['/nonexistent/oa-controller'], directory=directory, owner_task_id=str(uuid4()))
    assert receipt['state'] == 'start_failed'
    assert receipt['exit_code'] is None
    assert json.loads(Path(receipt['receipt_file']).read_text())['result'] == 'blocked'
    with pytest.raises(RuntimeError, match='writer_lock_unsafe'):
        supervise([sys.executable], directory=directory, owner_task_id=str(uuid4()))


def test_broken_matching_manifest_still_retains_exit(tmp_path):
    code = '''import os,json,datetime
from pathlib import Path
Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE']).write_text(json.dumps({
 'kind':'preaudit_controller_run_manifest','controller_pid':os.getpid(),
 'run_id':'a'*32,'controller_instance_id':'b'*32,
 'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}))'''
    receipt = run(tmp_path, code)
    assert receipt['exit_code'] == 0
    assert receipt['result'] == 'blocked'
    assert json.loads(Path(receipt['receipt_file']).read_text())['state'] == 'exited'


def test_parent_symlink_rejected_before_spawn(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(RuntimeError, match='writer_lock_unsafe'):
        supervise([sys.executable], directory=link / uuid4().hex, owner_task_id=str(uuid4()))
    assert list(actual.iterdir()) == []


def test_signal_requests_stop_only_for_owned_controller(tmp_path, monkeypatch):
    from apps.oa_approval import runtime_control
    stops = []
    monkeypatch.setattr(runtime_control, 'request_cycle_stop', lambda: stops.append(True))
    monkeypatch.setattr(runtime_control, 'controller_owner_pid', lambda: None)
    code = 'import os,signal,time; os.kill(os.getppid(),signal.SIGTERM); time.sleep(.1)'
    receipt = run(tmp_path, code)
    assert receipt['received_signals'] == [signal.SIGTERM]
    assert receipt['exit_code'] == 0  # Supervisor does not kill an in-flight Writer.
    assert not stops


def test_real_handoff_mirror_and_rolling_entry_wiring(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import controller_handoff
    from apps.oa_approval import runtime_control
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    directory = tmp_path / 'executions' / uuid4().hex
    directory.mkdir(parents=True)
    mirror = directory / 'controller-manifest.json'
    monkeypatch.setenv('OA_CONTROLLER_EXECUTION_MANIFEST_FILE', str(mirror))
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({'capacity': 1})
        assert json.loads(mirror.read_text()) == manifest
        controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    assert json.loads(mirror.read_text())['state'] == 'completed'


def test_mirror_failure_does_not_create_global_running(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import controller_handoff
    from apps.oa_approval import runtime_control
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    monkeypatch.setenv('OA_CONTROLLER_EXECUTION_MANIFEST_FILE', str(tmp_path / 'bad.json'))
    with runtime_control.global_controller_lease():
        with pytest.raises(ValueError, match='controller_execution_evidence_failed'):
            controller_handoff.begin({'capacity': 1})
    assert not controller_handoff.manifest_file().exists()


def test_supervisor_cancel_uses_controller_stop_and_waits(tmp_path, monkeypatch):
    from scripts import finance_preaudit_run
    from apps.oa_approval import runtime_control
    real_popen = finance_preaudit_run.subprocess.Popen
    owned = []
    stops = []

    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        owned.append(process.pid)
        return process

    monkeypatch.setattr(finance_preaudit_run.subprocess, 'Popen', start)
    monkeypatch.setattr(runtime_control, 'controller_owner_pid', lambda: owned[0] if owned else None)
    monkeypatch.setattr(runtime_control, 'request_cycle_stop', lambda: stops.append(True))
    receipt = run(tmp_path, 'import os,signal,time; os.kill(os.getppid(),signal.SIGTERM); time.sleep(1.1)')
    assert stops == [True]  # A completed controller may already have cleared this stop.
    assert receipt['exit_code'] == 0


def test_main_calls_original_entry_with_original_write_gate(tmp_path, monkeypatch):
    from scripts import finance_preaudit_run
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    # No write confirmation: the real original entry rejects before any OA work.
    assert finance_preaudit_run.main(['--owner-task-id', str(uuid4()), '--']) == 1
    receipt_file, = tmp_path.glob('executions/*/execution.json')
    receipt = json.loads(receipt_file.read_text())
    assert receipt['state'] == 'exited' and receipt['exit_code'] == 1
    output = json.loads(receipt_file.with_name('stdout.log').read_text())
    assert output['error_code'] == 'production_probe_write_confirmation_required'


def test_stop_failure_does_not_abandon_child(tmp_path, monkeypatch):
    from scripts import finance_preaudit_run
    from apps.oa_approval import runtime_control
    real_popen = finance_preaudit_run.subprocess.Popen
    owned = []
    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        owned.append(process.pid)
        return process
    def broken_stop():
        raise RuntimeError('control_file_unsafe')
    monkeypatch.setattr(finance_preaudit_run.subprocess, 'Popen', start)
    monkeypatch.setattr(runtime_control, 'controller_owner_pid', lambda: owned[0] if owned else None)
    monkeypatch.setattr(runtime_control, 'request_cycle_stop', broken_stop)
    receipt = run(tmp_path, 'import os,signal,time; os.kill(os.getppid(),signal.SIGTERM); time.sleep(.6)')
    assert receipt['state'] == 'exited' and receipt['exit_code'] == 0
    assert receipt['stop_request_error'] == 'controller_stop_request_failed'
    assert receipt['result'] == 'blocked'


def test_main_loads_control_paths_before_selecting_evidence_directory(tmp_path, monkeypatch):
    from scripts import finance_preaudit_run
    monkeypatch.setattr(finance_preaudit_run.auth, 'load_local_env', lambda: monkeypatch.setenv(
        'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'configured.lock')))
    captured = []
    def fake(command, *, directory, owner_task_id):
        captured.append(directory)
        return {'result': 'completed'}
    monkeypatch.setattr(finance_preaudit_run, 'supervise', fake)
    assert finance_preaudit_run.main(['--owner-task-id', str(uuid4()), '--']) == 0
    assert captured[0].parent == tmp_path / 'executions'
