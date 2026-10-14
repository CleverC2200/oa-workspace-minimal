import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from scripts.finance_preaudit_run import supervise


def recorded_execution(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    owner = str(uuid4())
    directory = tmp_path / 'executions' / uuid4().hex
    code = '''import os,json,datetime
from pathlib import Path
Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE']).write_text(json.dumps({
 'kind':'preaudit_controller_run_manifest','controller_pid':os.getpid(),
 'run_id':'a'*32,'controller_instance_id':'b'*32,'state':'completed',
 'children_reaped':True,'code_revision':'c'*40,
 'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}))
print('{"kind":"synthetic_result","private_customerdata":"local-only"}')'''
    receipt = supervise([sys.executable, '-c', code], directory=directory, owner_task_id=owner)
    return Path(receipt['receipt_file']), owner


def readback(path, owner, *args):
    return subprocess.run([sys.executable, '-B', '-m', 'scripts.finance_preaudit_run',
                           '--owner-task-id', owner, '--receipt-file', str(path),
                           '--invocation-id', path.parent.name, *args],
                          capture_output=True, text=True, env=dict(os.environ))


def test_progress_and_terminal_retrieval_use_the_original_receipt(tmp_path, monkeypatch):
    path, owner = recorded_execution(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in path.parent.iterdir()}
    progress = readback(path, owner, '--progress')
    assert progress.returncode == 0, progress.stderr + progress.stdout
    report = json.loads(progress.stdout)
    assert report['status'] == 'completed' and report['terminal'] is True
    assert report['tool_session_id'] == 'unknown'
    assert 'private_customerdata' not in progress.stdout
    result = readback(path, owner, '--retrieve-result')
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)['controller_stdout'] == '{"kind":"synthetic_result","private_customerdata":"local-only"}\n'
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize('damage', ['owner', 'invocation', 'pid', 'run', 'revision', 'old_manifest',
                                     'future_manifest', 'stdout', 'stderr', 'receipt_symlink',
                                     'mirror_symlink', 'binding_owner', 'binding_session', 'duplicate'])
def test_readback_rejects_mismatched_or_tampered_evidence(tmp_path, monkeypatch, damage):
    path, owner = recorded_execution(tmp_path, monkeypatch)
    mirror = path.with_name('controller-manifest.json')
    if damage in {'owner', 'invocation'}:
        receipt = json.loads(path.read_text())
        receipt['owner_task_id' if damage == 'owner' else 'invocation_id'] = uuid4().hex
        path.write_text(json.dumps(receipt))
    elif damage in {'pid', 'run', 'revision', 'old_manifest', 'future_manifest'}:
        manifest = json.loads(mirror.read_text())
        key, value = {'pid': ('controller_pid', 123), 'run': ('run_id', 'd' * 32),
                      'revision': ('code_revision', 'e' * 40),
                      'old_manifest': ('started_at', '2000-01-01T00:00:00+00:00'),
                      'future_manifest': ('started_at', '2099-01-01T00:00:00+00:00')}[damage]
        manifest[key] = value
        mirror.write_text(json.dumps(manifest))
        # Older receipts lack the new mirror hash; chronology must still match the actual wait.
        receipt = json.loads(path.read_text()); receipt.pop('manifest_sha256')
        path.write_text(json.dumps(receipt))
    elif damage in {'stdout', 'stderr'}:
        path.with_name(damage + '.log').write_text('tampered')
    elif damage.endswith('symlink'):
        target = path if damage == 'receipt_symlink' else mirror
        target.rename(tmp_path / 'outside.json')
        target.symlink_to(tmp_path / 'outside.json')
    elif damage.startswith('binding_'):
        path.with_name('tool-session.json').write_text(json.dumps({
            'kind': 'preaudit_controller_tool_session', 'owner_task_id': str(uuid4()) if damage == 'binding_owner' else owner,
            'invocation_id': path.parent.name, 'tool_session_id': 'wrong-session',
            'evidence_source': 'caller_reported_tool_result'}))
    else:
        path.write_text(path.read_text().replace('"state": "exited"', '"state": "exited", "state": "exited"'))
    result = readback(path, owner, '--retrieve-result', *(['--tool-session-id', 'original'] if damage == 'binding_session' else []))
    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads(result.stdout)['error_code'] == 'controller_execution_evidence_failed'
    assert 'controller_stdout' not in result.stdout


@pytest.mark.parametrize('gap', ['waiting', 'missing_manifest', 'running_manifest', 'unreaped', 'unknown'])
def test_missing_unfinished_or_unreaped_execution_never_succeeds(tmp_path, monkeypatch, gap):
    path, owner = recorded_execution(tmp_path, monkeypatch)
    receipt = json.loads(path.read_text())
    mirror = path.with_name('controller-manifest.json')
    manifest = json.loads(mirror.read_text())
    if gap == 'missing_manifest':
        mirror.unlink()
    elif gap == 'waiting':
        receipt.update(state='waiting', completed_at=None, exit_code=None, exit_signal=None)
        receipt.pop('result'); receipt.pop('output_sha256')
        mirror.unlink()
    elif gap == 'unknown':
        receipt['children_reaped'] = 'unknown'
        mirror.unlink()
    else:
        manifest['state' if gap == 'running_manifest' else 'children_reaped'] = 'running' if gap == 'running_manifest' else False
        receipt['controller_state'] = manifest['state']; receipt['children_reaped'] = manifest['children_reaped']
        receipt.pop('manifest_sha256'); receipt['result'] = 'blocked'
        mirror.write_text(json.dumps(manifest))
    path.write_text(json.dumps(receipt))
    before = path.read_bytes()
    started = time.monotonic()
    result = readback(path, owner, '--retrieve-result', '--wait-seconds', '0.1')
    assert result.returncode == (2 if gap == 'waiting' else 1), result.stdout + result.stderr
    assert json.loads(result.stdout)['terminal'] is False
    assert 'controller_stdout' not in result.stdout
    assert time.monotonic() - started < 2
    assert path.read_bytes() == before


def test_readonly_cli_does_not_load_auth_or_start_any_child(tmp_path, monkeypatch, capsys):
    from scripts import finance_preaudit_run as runner
    path, owner = recorded_execution(tmp_path, monkeypatch)
    capsys.readouterr()
    def forbidden(*args, **kwargs):
        raise AssertionError('readonly readback touched auth or controller')
    monkeypatch.setattr(runner.auth, 'load_local_env', forbidden)
    monkeypatch.setattr(runner.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(runner.runtime_control, 'request_cycle_stop', forbidden)
    assert runner.main(['--owner-task-id', owner, '--receipt-file', str(path),
                        '--invocation-id', path.parent.name, '--progress']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'completed'


@pytest.mark.parametrize('seconds', ['-1', '61', 'nan', 'inf'])
def test_wait_limit_rejects_unbounded_requests(tmp_path, monkeypatch, seconds):
    path, owner = recorded_execution(tmp_path, monkeypatch)
    result = readback(path, owner, '--progress', '--wait-seconds', seconds)
    assert result.returncode == 1


def test_non_regular_evidence_is_rejected_without_blocking(tmp_path, monkeypatch):
    path, owner = recorded_execution(tmp_path, monkeypatch)
    path.with_name('stdout.log').unlink()
    os.mkfifo(path.with_name('stdout.log'))
    result = subprocess.run([sys.executable, '-B', '-m', 'scripts.finance_preaudit_run', '--owner-task-id', owner,
                             '--receipt-file', str(path), '--invocation-id', path.parent.name, '--progress'],
                            capture_output=True, text=True, timeout=2)
    assert result.returncode == 1
    assert json.loads(result.stdout)['error_code'] == 'controller_execution_evidence_failed'


def test_large_verified_logs_keep_terminal_readback_bounded(tmp_path, monkeypatch):
    import hashlib
    path, owner = recorded_execution(tmp_path, monkeypatch)
    raw = b'x' * (2 * 1024 * 1024)
    receipt = json.loads(path.read_text())
    for name in ('stdout.log', 'stderr.log'):
        path.with_name(name).write_bytes(raw)
        receipt['output_sha256'][name] = hashlib.sha256(raw).hexdigest()
    path.write_text(json.dumps(receipt))
    result = readback(path, owner, '--retrieve-result')
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report['status'] == 'completed' and report['terminal'] is True
    assert report['output_omitted'] == 'size_limit'
    assert report['controller_stdout_file'] == str(path.with_name('stdout.log'))
    assert report['output_files']['stdout.log']['size_bytes'] == 2097152
    assert len(result.stdout) < 4096


def test_verified_skip_retains_original_non_takeover_result(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    payload = {'kind': 'preaudit_production_rolling_drain', 'status': 'skipped',
               'reason': 'production_rolling_controller_active', 'controller_handoff': {
                   'status': 'active', 'manifest_state': 'running', 'controller_active': True,
                   'lock_owner_consistent': True, 'restart_verified': True, 'code_matches_current': True}}
    owner = str(uuid4())
    receipt = supervise([sys.executable, '-c', 'print(' + repr(json.dumps(payload)) + ')'],
                        directory=tmp_path / 'executions' / uuid4().hex, owner_task_id=owner)
    result = readback(Path(receipt['receipt_file']), owner, '--retrieve-result')
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report['status'] == 'skipped' and report['terminal'] is True
    assert report['children_reaped'] == 'unknown'
    assert 'run_id' not in report


def test_wait_observes_original_supervisor_completion(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'rolling.lock'))
    owner = str(uuid4())
    directory = tmp_path / 'executions' / uuid4().hex
    child = '''import os,json,datetime,time
from pathlib import Path
from apps.oa_approval import runtime_control
time.sleep(.3)
runtime_control.write_control_bytes_atomic(Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE']), json.dumps({
 'kind':'preaudit_controller_run_manifest','controller_pid':os.getpid(),
 'run_id':'a'*32,'controller_instance_id':'b'*32,'state':'completed',
 'children_reaped':True,'code_revision':'c'*40,
 'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}).encode())
print('original terminal output')'''
    code = ('from pathlib import Path; import sys; from scripts.finance_preaudit_run import supervise; '
            f'supervise([sys.executable, "-c", {child!r}], directory=Path({str(directory)!r}), owner_task_id={owner!r})')
    with subprocess.Popen([sys.executable, '-B', '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as supervisor:
        started = json.loads(supervisor.stdout.readline())
        assert started['state'] == 'waiting'
        result = readback(directory / 'execution.json', owner, '--retrieve-result', '--wait-seconds', '2')
        assert result.returncode == 0, result.stderr + result.stdout
        assert json.loads(result.stdout)['controller_stdout'] == 'original terminal output\n'
        supervisor.communicate(timeout=2)
        assert supervisor.returncode == 0


def test_matching_original_binding_is_reported_and_must_not_be_guessed(tmp_path, monkeypatch):
    from scripts.finance_preaudit_run import bind_tool_session
    path, owner = recorded_execution(tmp_path, monkeypatch)
    unbound = readback(path, owner, '--progress', '--tool-session-id', '82550')
    assert unbound.returncode == 1
    bind_tool_session(path, owner_task_id=owner, session_id='82550')
    bound = readback(path, owner, '--progress', '--tool-session-id', '82550')
    assert bound.returncode == 0
    assert json.loads(bound.stdout)['tool_session_id'] == '82550'


@pytest.mark.parametrize('artifact', ['execution.json', 'controller-manifest.json', 'tool-session.json'])
def test_oversized_control_artifacts_fail_closed(tmp_path, monkeypatch, artifact):
    path, owner = recorded_execution(tmp_path, monkeypatch)
    path.with_name(artifact).write_bytes(b' ' * (1048576 + 1))
    result = readback(path, owner, '--progress')
    assert result.returncode == 1
    assert json.loads(result.stdout)['error_code'] == 'controller_execution_evidence_failed'


@pytest.mark.parametrize('field,value', [('schema_version', True), ('children_reaped', 1), ('process_group_id', 999)])
def test_malformed_receipt_types_and_process_identity_are_rejected(tmp_path, monkeypatch, field, value):
    path, owner = recorded_execution(tmp_path, monkeypatch)
    receipt = json.loads(path.read_text()); receipt[field] = value
    path.write_text(json.dumps(receipt))
    result = readback(path, owner, '--progress')
    assert result.returncode == 1
    assert json.loads(result.stdout)['error_code'] == 'controller_execution_evidence_failed'
