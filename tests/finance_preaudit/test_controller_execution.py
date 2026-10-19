import json
import os
from pathlib import Path
import signal
import sys
from uuid import uuid4

import pytest
from apps.oa_approval import runtime_control

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
    monkeypatch.setattr(finance_preaudit_run.approval_lifecycle, 'startup', lambda _: {'status': 'ready'})
    monkeypatch.setattr(finance_preaudit_run.approval_guard, 'observe_project_errors', lambda _: None)
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


def test_official_stop_sets_bounded_readback_without_killing_pending_controller(tmp_path, monkeypatch):
    from scripts import finance_preaudit_run
    from apps.oa_finance_preaudit import approval_guard
    real = finance_preaudit_run.subprocess.Popen
    pids = []
    def synthetic(_command, **kwargs):
        child = real([sys.executable, '-c', 'import time;time.sleep(.8)'], **kwargs)
        pids.append(child.pid)
        return child
    monkeypatch.setattr(finance_preaudit_run.subprocess, 'Popen', synthetic)
    monkeypatch.setattr(finance_preaudit_run, 'STOP_GRACE_SECONDS', .05)
    monkeypatch.setattr(runtime_control, 'controller_owner_pid', lambda: pids[0] if pids else None)
    monkeypatch.setattr(approval_guard, 'observe_project_errors', lambda _: approval_guard.record_notice('usage_limit', 'official-main'))
    receipt = finance_preaudit_run.supervise([sys.executable, '-m', 'scripts.finance_preaudit_concurrency_probe'],
        directory=tmp_path / uuid4().hex, owner_task_id=str(uuid4()))
    assert receipt['stop_wait_error'] == 'controller_stop_timeout'
    assert receipt['exit_code'] == 0 and receipt['result'] == 'blocked'
    assert runtime_control.stop_reason() == approval_guard.STOP_REASON


def test_main_loads_control_paths_before_selecting_evidence_directory(tmp_path, monkeypatch):
    from scripts import finance_preaudit_run
    monkeypatch.setattr(finance_preaudit_run.approval_lifecycle, 'startup', lambda _: {'status': 'ready'})
    monkeypatch.setattr(finance_preaudit_run.auth, 'load_local_env', lambda: monkeypatch.setenv(
        'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'configured.lock')))
    captured = []
    def fake(command, *, directory, owner_task_id, detached):
        assert detached is True
        captured.append(directory)
        return {'result': 'completed'}
    monkeypatch.setattr(finance_preaudit_run, 'supervise', fake)
    assert finance_preaudit_run.main(['--owner-task-id', str(uuid4()), '--']) == 0
    assert captured[0].parent == tmp_path / 'executions'


@pytest.mark.parametrize('trusted', [True, False])
def test_concurrent_start_race_preserves_original_verified_skip(tmp_path, trusted):
    payload = {'kind': 'preaudit_production_rolling_drain', 'status': 'skipped',
        'reason': 'production_rolling_controller_active', 'controller_handoff': {
        'status': 'active', 'manifest_state': 'running', 'controller_active': True,
        'lock_owner_consistent': trusted, 'restart_verified': True, 'code_matches_current': True}}
    code = 'print(' + repr(json.dumps(payload)) + ')'
    receipt = run(tmp_path, code)
    assert receipt['result'] == ('skipped' if trusted else 'blocked')
    assert receipt['children_reaped'] == 'unknown'  # Other controller remains owned by its original caller.
    assert receipt['run_id'] is None


@pytest.mark.parametrize('reason', ['previous_run_busy', 'approval_model_cooldown'])
def test_verified_startup_skip_stays_terminal_when_live_gate_changes(tmp_path, monkeypatch, reason):
    from scripts import finance_preaudit_run as runner
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    payload = {'kind': 'approval_startup_gate', 'status': 'skipped', 'reason': reason}
    if reason == 'approval_model_cooldown':
        payload['blocked_until'] = 10000
        monkeypatch.setattr(runner.approval_guard, 'cooldown', lambda: {'blocked_until': 10000})
    else:
        payload['run_id'] = 'a' * 32
        monkeypatch.setattr(runner.controller_handoff, '_read_manifest', lambda: {'run_id': 'a' * 32, 'controller_pid': 123})
        monkeypatch.setattr(runner.approval_lifecycle, '_progress', lambda _: {'controller': {'pid': 123}})
        monkeypatch.setattr(runtime_control, 'controller_active', lambda: True)
        monkeypatch.setattr(runtime_control, 'controller_owner_pid', lambda: 123)
        identity = runner.approval_guard.process_identity
        monkeypatch.setattr(runner.approval_guard, 'process_identity', lambda pid: {'pid': 123} if pid == 123 else identity(pid))
    owner = str(uuid4()); directory = tmp_path / 'executions' / uuid4().hex
    receipt = runner.supervise([sys.executable, '-c', 'print(' + repr(json.dumps(payload)) + ')'],
                               directory=directory, owner_task_id=owner)
    assert receipt['result'] == 'skipped'
    monkeypatch.setattr(runner, '_verified_skip', lambda _: pytest.fail('historical skip must not read live gates'))
    options = {'owner_task_id': owner, 'invocation_id': directory.name}
    report = runner.read_execution(directory / 'execution.json', **options)
    assert report['status'] == 'skipped' and report['terminal'] is True
    saved = json.loads((directory / 'execution.json').read_text())
    saved['skip_verification']['stdout_sha256'] = '0' * 64
    runtime_control.write_control_bytes_atomic(directory / 'execution.json', json.dumps(saved).encode())
    assert runner.read_execution(directory / 'execution.json', **options)['terminal'] is False


def test_skipped_stdout_cannot_override_corrupt_existing_mirror(tmp_path):
    payload = {'kind': 'preaudit_production_rolling_drain', 'status': 'skipped',
        'reason': 'production_rolling_controller_active', 'controller_handoff': {
        'status': 'active', 'manifest_state': 'running', 'controller_active': True,
        'lock_owner_consistent': True, 'restart_verified': True, 'code_matches_current': True}}
    code = "import os; from pathlib import Path; Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE']).write_text('{}'); print(" + repr(json.dumps(payload)) + ')'
    receipt = run(tmp_path, code)
    assert receipt['result'] == 'blocked' and receipt['exit_code'] == 0


def test_original_tool_session_binding_is_exclusive_and_not_overwritten(tmp_path, monkeypatch):
    from scripts import finance_preaudit_run as runner
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    owner = str(uuid4()); directory = tmp_path / 'executions' / uuid4().hex
    receipt = runner.supervise([sys.executable, '-c', 'pass'], directory=directory, owner_task_id=owner)
    path = Path(receipt['receipt_file'])
    before = path.read_bytes()
    result = runner.bind_tool_session(path, owner_task_id=owner, session_id='82550')
    assert result['status'] == 'bound' and path.read_bytes() == before
    assert runner.bind_tool_session(path, owner_task_id=owner, session_id='82550') == result
    assert json.loads(Path(result['tool_session_file']).read_text())['evidence_source'] == 'caller_reported_tool_result'
    with pytest.raises(ValueError, match='controller_tool_session_binding_conflict'):
        runner.bind_tool_session(path, owner_task_id=owner, session_id='different')
    with pytest.raises(ValueError, match='controller_tool_session_binding_invalid'):
        runner.bind_tool_session(path, owner_task_id=str(uuid4()), session_id='82550')


@pytest.mark.parametrize('targeted', [False, True])
def test_public_startup_preserves_active_foreign_app_controller(tmp_path, monkeypatch, capsys, targeted):
    import subprocess
    from scripts import finance_preaudit_run as runner
    from tests.finance_preaudit.test_controller_owner_lifecycle import wait_until

    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    monkeypatch.setenv('OA_REVIEW_TRANSPORT', 'host_chat')
    monkeypatch.setattr(runner.auth, 'load_local_env', lambda: None)
    monkeypatch.setattr(runner.approval_guard, 'observe_project_errors', lambda _: None)
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    directory = tmp_path / 'executions' / uuid4().hex
    command = [sys.executable, '-c', f'''import time
from pathlib import Path
from apps.oa_approval import runtime_control as control
from apps.oa_finance_preaudit import controller_handoff as handoff,approval_lifecycle as lifecycle,host_chat_review as host
with control.global_controller_lease():
    manifest=handoff.begin({{'review_configuration':host.IDENTITY}})
    with lifecycle.run_scope(manifest,Path.cwd()):
        Path({str(ready)!r}).touch()
        while not Path({str(release)!r}).exists():time.sleep(.02)
    handoff.finish(manifest,status='completed',children_reaped=True,health='healthy')
''']
    wrapper = f'from pathlib import Path\nfrom scripts.finance_preaudit_run import supervise\nsupervise({command!r},directory=Path({str(directory)!r}),owner_task_id={str(uuid4())!r})'
    process = subprocess.Popen([sys.executable, '-c', wrapper], cwd=runner.PROJECT_ROOT,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_until(ready.exists)
        original = (directory / 'execution.json').read_bytes()
        manifest_path = directory / 'controller-manifest.json'
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        assert manifest['state'] == 'running' and manifest['review_configuration']['transport'] == 'host_chat'
        assert runner.approval_lifecycle.startup(runner.PROJECT_ROOT)['reason'] == 'previous_run_busy'
        calls = []
        def supervised(command, **options):
            assert targeted, 'ordinary busy startup must skip before launch'
            calls.append(command)
            return {'result': 'skipped'}
        monkeypatch.setattr(runner, 'supervise', supervised)
        options = ['--targets-only', '--resume-action-file', str(tmp_path / 'action.json')] if targeted else []
        assert runner.main(['--owner-task-id', str(uuid4()), '--', '--rolling', *options]) == 0
        output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        if targeted:
            assert calls and '--targets-only' in calls[0]
        else:
            assert output['status'] == 'skipped' and output['reason'] == 'previous_run_busy'
            assert output['run_id'] == manifest['run_id'] and not calls
        assert not runtime_control.stop_requested()
        assert runtime_control.controller_active() and process.poll() is None
        assert (directory / 'execution.json').read_bytes() == original
        assert manifest_path.read_bytes() == manifest_bytes
        assert not (tmp_path / 'approval-guard/cooldown-overrides').exists()
        assert len(list((tmp_path / 'executions').iterdir())) == 1
    finally:
        release.touch()
        assert process.wait(timeout=6) == 0
