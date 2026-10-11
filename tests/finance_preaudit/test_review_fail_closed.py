import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
from uuid import uuid4

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import codex_review_worker as worker
from apps.oa_finance_preaudit import approval_guard as guard
from scripts import finance_preaudit_run, finance_preaudit_concurrency_probe as probe
from tests.finance_preaudit.test_codex_review_worker import _task, _small_marketing_v2_task
from tests.finance_preaudit.test_concurrency_probe import _open_action
from tests.finance_preaudit.test_execution_readback import recorded_execution


def test_approval_cli_disables_unbounded_connection_retries(tmp_path):
    command = worker._codex_command(tmp_path, tmp_path / 'schema', tmp_path / 'decision')
    assert 'features.unbounded_connection_retries=false' in command
    assert 'model_provider="openai"' in command


@pytest.mark.parametrize('code', ['codex_process_failed', 'codex_process_incomplete', 'codex_process_timeout'])
def test_first_technical_failure_stops_without_retry(tmp_path, code):
    task, action = _task(tmp_path)
    calls = []

    def execute(*args):
        calls.append(args)
        return worker.CodexExecution(code, {'status': 'unknown'})

    result = worker.review(task, executor=execute)
    assert len(calls) == 1
    assert result['error_code'] == code
    assert result['process_retries'] == 0
    assert runtime_control.stop_requested()
    assert runtime_control.stop_reason() == 'codex_review_technical_failure'
    assert action.with_name('review-failure.json').exists()
    assert not action.with_name('execution-receipt.json').exists()


def test_stop_blocks_next_review_before_model_execution(tmp_path):
    task, _ = _task(tmp_path)
    runtime_control.request_stop()

    def unexpected(*args):
        pytest.fail('a stopped run started another model request')

    result = worker.review(task, executor=unexpected)
    assert result['error_code'] == 'stop_requested'
    assert result['process_attempts'] == 0


def test_stop_during_prompt_preparation_blocks_popen(tmp_path, monkeypatch):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))

    def prompt(_action):
        runtime_control.request_stop()
        return 'offline-only prompt'

    def unexpected(*args, **kwargs):
        pytest.fail('a stop during preparation still spawned a model CLI')

    monkeypatch.setattr(worker.subprocess, 'Popen', unexpected)
    result = worker._execute_codex_raw(
        Path(task['project_root']), action, 60, prompt_builder=prompt, stderr_summary={})
    assert result.error_code == 'stop_requested'
    assert result.process_state == 'not_started'


def test_technical_stop_upgrades_cycle_stop_and_survives_cycle_cleanup():
    runtime_control.request_cycle_stop()
    runtime_control.request_stop('codex_review_technical_failure')
    assert runtime_control.stop_reason() == 'codex_review_technical_failure'
    assert runtime_control.clear_cycle_stop() is False
    assert runtime_control.stop_requested()


def test_existing_manual_stop_is_never_replaced():
    runtime_control.request_stop()
    original = runtime_control.stop_file().read_bytes()
    runtime_control.request_stop('codex_review_technical_failure')
    assert runtime_control.stop_file().read_bytes() == original


def test_other_worker_cannot_upgrade_unowned_technical_stop(tmp_path):
    _task_a, first = _task(tmp_path / 'a')
    _task_b, second = _task(tmp_path / 'b')
    identity = worker._request_review_stop(first, 'codex_process_failed')
    assert identity is not None
    assert worker._request_review_stop(second, 'codex_process_failed') is None
    guard.record_notice('usage_limit', 'second-worker', technical_stop_identity=None)
    assert runtime_control.stop_reason() == 'codex_review_technical_failure'
    # A later replacement is not the marker that the first worker published.
    runtime_control.write_control_bytes_atomic(runtime_control.stop_file(), b'stop_requested:codex_review_technical_failure\n')
    guard.record_notice('usage_limit', 'first-worker', technical_stop_identity=identity)
    assert runtime_control.stop_reason() == 'codex_review_technical_failure'


def test_stop_publication_waits_for_launch_lease():
    entered = threading.Event()
    done = threading.Event()

    def stop():
        entered.set()
        runtime_control.request_stop('codex_review_technical_failure')
        done.set()

    with runtime_control.stop_control_lease():
        thread = threading.Thread(target=stop)
        thread.start()
        assert entered.wait(1)
        assert not done.wait(0.1)
        assert not runtime_control.stop_requested()
    thread.join(timeout=2)
    assert done.is_set()
    assert runtime_control.stop_requested()


def test_waiting_receipt_with_dead_supervisor_is_blocked_not_success(tmp_path, monkeypatch):
    path, owner = recorded_execution(tmp_path, monkeypatch)
    receipt = json.loads(path.read_text())
    receipt.update(state='waiting', supervisor_pid=2_000_000_000, exit_code=None,
                   exit_signal=None, completed_at=None)
    path.write_text(json.dumps(receipt))
    original = path.read_bytes()
    report = finance_preaudit_run.wait_execution(
        path, owner_task_id=owner, invocation_id=path.parent.name)
    assert report['status'] == 'blocked'
    assert report['error_code'] == 'controller_supervisor_lost'
    assert report['terminal'] is False
    assert report['exit_code'] is None
    assert path.read_bytes() == original


def test_stop_wait_timeout_preserves_actual_exit_and_reports_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(finance_preaudit_run, 'STOP_GRACE_SECONDS', 0.05)
    monkeypatch.setattr(runtime_control, 'controller_owner_pid', lambda: None)
    receipt = finance_preaudit_run.supervise([
        sys.executable, '-c',
        'import os,signal,time; os.kill(os.getppid(),signal.SIGTERM); time.sleep(1.2)',
    ], directory=tmp_path / uuid4().hex, owner_task_id=str(uuid4()))
    assert receipt['state'] == 'exited'
    assert receipt['exit_code'] == 0
    assert receipt['stop_wait_error'] == 'controller_stop_timeout'
    assert receipt['result'] == 'blocked'


@pytest.mark.parametrize('event', [
    {'type': 'error', 'message': 'model unavailable'},
    {'type': 'turn.failed', 'error': {'message': 'server overloaded'}},
    {'type': 'item.completed', 'item': {'type': 'error', 'message': 'usage limit exceeded'}},
])
def test_live_error_stops_hanging_cli_before_total_timeout(tmp_path, monkeypatch, event):
    task, action = _task(tmp_path)
    task['timeout_seconds'] = 60
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    monkeypatch.setattr(worker, '_codex_command', lambda *args, **kwargs: [
        sys.executable, '-c',
        'import sys,time; sys.stdin.read(); print(' + repr(json.dumps(event)) +
        ', flush=True); time.sleep(10)',
    ])
    started = time.monotonic()
    result = worker.review(task)
    assert time.monotonic() - started < 8
    assert result['error_code'] == 'codex_process_failed'
    assert runtime_control.stop_requested()
    assert result['process_attempts'] == 1


def test_other_worker_stop_interrupts_live_cli(tmp_path, monkeypatch):
    task, _ = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    monkeypatch.setattr(worker, '_codex_command', lambda *args, **kwargs: [
        sys.executable, '-c', 'import sys,time; sys.stdin.read(); time.sleep(10)',
    ])

    def stop():
        time.sleep(0.2)
        runtime_control.request_stop()

    stopper = threading.Thread(target=stop)
    stopper.start()
    try:
        started = time.monotonic()
        result = worker.review(task)
        assert time.monotonic() - started < 8
        assert result['error_code'] == 'stop_requested'
        assert result['process_attempts'] == 1
    finally:
        stopper.join()


@pytest.mark.parametrize('channel', ['stdout', 'stderr', 'curly_stderr', 'split_stderr', 'backlogged_stdout'])
def test_first_usage_warning_stops_before_ten_second_retry(tmp_path, monkeypatch, channel):
    task, _ = _task(tmp_path)
    retry = tmp_path / 'retry-started'
    child_pid = tmp_path / 'model-pid'
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    warning = "You've hit your usage limit. Try again later."
    event = json.dumps({'type': 'error', 'message': warning})
    if channel == 'stdout':
        emit = f'print({event!r}, flush=True); '
    elif channel in {'stderr', 'curly_stderr'}:
        text = warning.replace("'", '\u2019') if channel == 'curly_stderr' else warning
        emit = f'print({text!r}, file=sys.stderr, end="", flush=True); '
    elif channel == 'split_stderr':
        emit = ('sys.stderr.write("You\\\'ve hit your usage "); sys.stderr.flush(); '
                'time.sleep(.1); sys.stderr.write("limit. Try again later."); sys.stderr.flush(); ')
    else:
        # Error lies beyond the old 512 KiB one-poll read limit.
        progress = json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution',
                                                                    'aggregated_output': 'x' * 1024}})
        emit = f'sys.stdout.write(({progress!r} + "\\n") * 1600); print({event!r}, flush=True); '
    script = ('import os,sys,time; from pathlib import Path; sys.stdin.read(); '
              f'Path({str(child_pid)!r}).write_text(str(os.getpid())); ' + emit +
              f'time.sleep(10); Path({str(retry)!r}).write_text("retry"); time.sleep(10)')
    monkeypatch.setattr(worker, '_codex_command', lambda *args, **kwargs: [sys.executable, '-c', script])
    started = time.monotonic()
    result = worker.review(task)
    assert worker.OBSERVATION_INTERVAL_SECONDS == 5
    assert time.monotonic() - started < 8
    assert result['error_code'] == 'codex_process_failed'
    assert result['process_attempts'] == 1
    assert result['process_retries'] == 0
    assert runtime_control.stop_requested()
    assert not retry.exists()
    assert runtime_control.stop_reason() == guard.STOP_REASON
    assert guard.cooldown()['blocked_until'] - guard.cooldown()['triggered_at'] == 10800
    with pytest.raises(ProcessLookupError):
        os.kill(int(child_pid.read_text()), 0)
    diagnostic = next(Path(task['action_file']).parent.glob('review-process-*.json')).read_text()
    assert warning not in diagnostic
    assert 'usage_limit_signal' in diagnostic


def test_quota_warning_classified_after_fast_exit_still_blocks_decision(tmp_path, monkeypatch):
    task, _ = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    original_summary = worker.process_diagnostics.stderr_summary

    def delayed_summary(stream, **kwargs):
        if 'on_signals' in kwargs:
            time.sleep(.3)
        return original_summary(stream, **kwargs)

    monkeypatch.setattr(worker.process_diagnostics, 'stderr_summary', delayed_summary)

    def command(root, schema, decision, **kwargs):
        return [sys.executable, '-c',
                'import sys,json; from pathlib import Path; sys.stdin.read(); '
                f'Path({str(decision)!r}).write_text(json.dumps({{"overall":"pass"}})); '
                'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}), flush=True); '
                'print("usage limit exceeded", file=sys.stderr, flush=True)']

    monkeypatch.setattr(worker, '_codex_command', command)
    result = worker.review(task)
    assert result['status'] == 'failed'
    assert result['error_code'] == 'codex_process_failed'
    assert result['process_retries'] == 0
    assert runtime_control.stop_requested()


def test_late_quota_classification_after_generic_stdout_failure_publishes_cooldown(tmp_path, monkeypatch):
    task, _ = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    original_summary = worker.process_diagnostics.stderr_summary
    def delayed(stream, **kwargs):
        if 'on_signals' in kwargs:
            time.sleep(.3)
        return original_summary(stream, **kwargs)
    monkeypatch.setattr(worker.process_diagnostics, 'stderr_summary', delayed)
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c',
        'import sys,json;sys.stdin.read();print(json.dumps({"type":"error","message":"generic failure"}),flush=True);'
        'print("usage limit exceeded",file=sys.stderr,flush=True)'])
    result = worker.review(task)
    assert result['status'] == 'failed' and result['process_retries'] == 0
    assert guard.cooldown()['notice_code'] == 'usage_limit'
    assert runtime_control.stop_reason() == guard.STOP_REASON


def test_registration_write_failure_reaps_already_started_cli(tmp_path, monkeypatch):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', 'import time;time.sleep(60)'])
    identity = guard.process_identity
    started = []
    def failed_registration(slot, pid, command):
        started.append(pid)
        raise OSError('offline registration disk failure')
    monkeypatch.setattr(guard, 'register_model', failed_registration)
    result = worker._execute_codex_raw(Path(task['project_root']), action, 60,
        prompt_builder=lambda _: 'offline', stderr_summary={})
    assert result.process_state == 'cleanup_failed'
    assert len(started) == 1 and identity(started[0]) is None
    assert not guard.models_reaped()  # Persistent unknown intent is not falsely freed.


@pytest.mark.parametrize('live_error', [True, False])
def test_native_failure_preserves_error_when_stop_publication_fails(tmp_path, monkeypatch, live_error):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    event = 'print(\'{"type":"error"}\', flush=True); ' if live_error else ''
    monkeypatch.setattr(worker, '_codex_command', lambda *args, **kwargs: [
        sys.executable, '-c', 'import sys,time; sys.stdin.read(); ' + event + 'time.sleep(10)',
    ])

    def fail_stop(*args):
        raise OSError('offline injected stop publication failure')

    monkeypatch.setattr(worker, '_request_review_stop', fail_stop)
    execution = worker._execute_codex_raw(
        Path(task['project_root']), action, 60 if live_error else 0.3,
        prompt_builder=lambda _action: 'offline-only prompt', stderr_summary={})
    expected = 'codex_process_failed' if live_error else 'codex_process_timeout'
    assert execution.error_code == expected
    assert execution.stop_request_error == 'codex_review_stop_failed'
    assert execution.exit_code is not None
    result = worker.review(task, executor=lambda *args: execution)
    assert result['error_code'] == expected
    assert result['stop_request_error'] == 'codex_review_stop_failed'
    assert result['process_attempts'] == 1


def test_normal_progress_is_not_mistaken_for_model_failure(tmp_path, monkeypatch):
    task, _ = _small_marketing_v2_task(tmp_path, budget_account='宣传制作费')
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))

    def command(root, schema, decision, **kwargs):
        return [sys.executable, '-c',
                'import sys,json,time; from pathlib import Path; sys.stdin.read(); '
                'print(json.dumps({"type":"turn.started"}), flush=True); '
                'print(json.dumps({"type":"item.completed","item":{"type":"command_execution",'
                '"aggregated_output":"usage limit exceeded model unavailable"}}), flush=True); time.sleep(.2); '
                f'Path({str(decision)!r}).write_text(json.dumps({{"overall":"pass"}})); '
                'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}), flush=True)']

    monkeypatch.setattr(worker, '_codex_command', command)
    result = worker.review(task)
    assert result['status'] == 'reviewed'
    assert not runtime_control.stop_requested()


def test_controller_retains_failure_result_before_reaping_pool(tmp_path):
    _open_action(tmp_path / 'runtime' / 'finance_preaudit_flows',
                 case_key='a' * 64, created_at='2026-10-11T00:00:00+00:00')
    completed = []

    def review(task):
        runtime_control.request_stop('codex_review_technical_failure')
        time.sleep(0.2)
        completed.append(True)
        return {'status': 'failed', 'error_code': 'codex_process_failed',
                'write_state': 'not_requested', 'token_usage': {'status': 'unknown'}}

    def unexpected(*args):
        pytest.fail('the stopped controller entered the Writer')

    result = probe._run_dispatched_tasks(
        1, 1, project_root=tmp_path, renew_actions=lambda paths: paths,
        review_worker=review, complete_review=unexpected,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {'status': 'healthy'})
    assert completed == [True]
    assert result.outcomes[0].value['error_code'] == 'codex_process_failed'
