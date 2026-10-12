import hashlib
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


_TRANSPORT_FALLBACK_WARNING = (
    'Falling back from WebSockets to HTTPS transport. '
    'stream disconnected before completion: IO error: Broken pipe (os error 32)'
)


def test_approval_cli_disables_unbounded_connection_retries(tmp_path):
    command = worker._codex_command(tmp_path, tmp_path / 'schema', tmp_path / 'decision')
    assert 'features.unbounded_connection_retries=false' in command
    assert 'model_provider="openai"' in command


@pytest.mark.parametrize('code', ['codex_process_failed', 'codex_process_incomplete', 'codex_process_timeout'])
def test_first_item_technical_failure_does_not_retry_or_stop_round(tmp_path, code):
    task, action = _task(tmp_path)
    calls = []

    def execute(*args):
        calls.append(args)
        return worker.CodexExecution(code, {'status': 'unknown'})

    result = worker.review(task, executor=execute)
    assert len(calls) == 1
    assert result['error_code'] == code
    assert result['process_retries'] == 0
    assert not runtime_control.stop_requested()
    assert guard.cooldown() is None
    assert action.with_name('review-failure.json').exists()
    assert not action.with_name('execution-receipt.json').exists()


@pytest.mark.parametrize('mode', ['unknown_error', 'nonzero_exit', 'incomplete', 'timeout'])
def test_item_technical_failure_allows_next_native_review(tmp_path, monkeypatch, mode):
    first_task, first_action = _task(tmp_path / 'first')
    next_task, _ = _small_marketing_v2_task(tmp_path / 'next', budget_account='宣传制作费')
    if mode == 'timeout':
        first_task['timeout_seconds'] = 60
    launches = []

    def command(root, schema, decision, **kwargs):
        launches.append(root)
        if str(root) == first_task['project_root']:
            script = {
                'unknown_error': 'print(json.dumps({"type":"error","message":"unexpected client failure"}),flush=True); time.sleep(10)',
                'nonzero_exit': 'sys.exit(1)',
                'incomplete': 'print(json.dumps({"type":"turn.started"}),flush=True)',
                'timeout': 'time.sleep(90)',
            }[mode]
            return [sys.executable, '-c', 'import sys,json,time; sys.stdin.read(); ' + script]
        return [sys.executable, '-c', 'import sys,json; from pathlib import Path; sys.stdin.read(); '
                f'Path({str(decision)!r}).write_text(json.dumps({{"overall":"pass"}})); '
                'print(json.dumps({"type":"turn.completed"}),flush=True)']

    monkeypatch.setattr(worker, '_codex_command', command)
    failed = worker.review(first_task)
    assert failed['status'] == 'failed'
    assert failed['process_attempts'] == 1 and failed['process_retries'] == 0
    assert first_action.with_name('review-failure.json').exists()
    assert not first_action.with_name('execution-receipt.json').exists()
    assert json.loads(first_action.read_text())['action'] == 'review_required'
    assert not runtime_control.stop_requested()
    assert guard.cooldown() is None
    completed = worker.review(next_task)
    assert completed['status'] == 'reviewed'
    assert len(launches) == 2


@pytest.mark.parametrize('failure_round', [1, 3, 4])
def test_consecutive_item_failures_leave_pending_and_continue_dispatch(tmp_path, failure_round):
    actions = [_open_action(tmp_path / 'runtime' / 'finance_preaudit_flows', case_key=str(i) * 64,
                            created_at=f'2026-10-11T00:00:0{i}+00:00') for i in range(4)]
    reviews, writes = [], []

    def review(task):
        action = Path(task['action_file'])
        reviews.append(action)
        if action != actions[-1]:
            return {**worker._base_result(attempts=1, usages=[{'status': 'unknown'}]),
                    'codex_events': worker._unknown_events(1), 'status': 'failed',
                    'business_state': 'not_available', 'write_state': 'not_requested',
                    'error_code': 'codex_process_failed'}
        return {'status': 'reviewed', 'decision': {'overall': 'pass'}, 'process_attempts': 1,
                'process_retries': 0, 'token_usage': {'status': 'unknown'}}

    def complete(action, reviewed):
        writes.append(action)
        return {**reviewed, 'status': 'completed', 'write_state': 'submitted', 'error_code': None}

    def quarantine(*args):
        pytest.fail('an ordinary deferred failure was permanently isolated')

    result = probe._run_dispatched_tasks(
        4, 1, project_root=tmp_path, renew_actions=lambda paths: paths,
        review_worker=review, complete_review=complete, quarantine_review=quarantine,
        record_failure_round=lambda *args: {'production_failure_round': failure_round},
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {'status': 'healthy'})
    assert reviews == actions
    assert writes == [actions[-1]]
    assert [outcome.value['status'] for outcome in result.outcomes] == ['retry_pending'] * 3 + ['completed']
    assert not runtime_control.stop_requested()
    assert guard.cooldown() is None


def test_parent_loss_takes_priority_over_simultaneous_native_failure(tmp_path, monkeypatch):
    task, action = _task(tmp_path)
    real_parent = os.getppid()
    observed_failure = threading.Event()
    classify = worker.process_diagnostics.notice_payload

    def classify_and_lose_parent(event, **kwargs):
        result = classify(event, **kwargs)
        if result is not None and result['classification'] == 'unknown_error':
            observed_failure.set()
        return result

    monkeypatch.setattr(worker.process_diagnostics, 'notice_payload', classify_and_lose_parent)
    monkeypatch.setattr(worker.os, 'getppid', lambda: real_parent + int(observed_failure.is_set()))
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    script = ('import sys,time; sys.stdin.read(); '
              'print(\'{"type":"error","message":"unexpected client failure"}\',flush=True); time.sleep(15)')
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    result = worker.review(task)
    assert observed_failure.is_set()
    assert result['error_code'] == 'stop_requested'
    assert runtime_control.stop_reason() == 'codex_review_technical_failure'
    assert json.loads(action.with_name('review-stop-trigger.json').read_text())['error_code'] == 'codex_review_parent_lost'
    assert guard.cooldown() is None


def test_writer_rejected_decision_is_pending_without_second_review(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import decision_validation, flow_engine

    actions = [_open_action(tmp_path / 'runtime' / 'finance_preaudit_flows', case_key=str(i) * 64,
                            created_at=f'2026-10-11T00:00:0{i}+00:00') for i in range(2)]
    for action in actions:
        for path in (action, action.with_name('state.json')):
            payload = json.loads(path.read_text())
            payload['action_token'] = action.parent.parent.name
            path.write_text(json.dumps(payload))
    reviews, completions, writes = [], [], []

    def review(task):
        action = Path(task['action_file'])
        reviews.append(action)
        return {**worker._base_result(attempts=1, usages=[{'status': 'unknown'}]),
                'codex_events': worker._unknown_events(1), 'status': 'reviewed',
                'business_state': 'not_available', 'write_state': 'not_requested', 'error_code': None,
                'agent_wall_seconds': 1, 'decision': {'overall': 'needs_human'}}

    def complete(action, reviewed):
        completions.append(action)
        if action == actions[-1]:
            writes.append(action)
            return {**reviewed, 'status': 'completed', 'write_state': 'submitted', 'error_code': None}

        def validate(path, decision):
            with pytest.raises(decision_validation.InvalidDecision) as rejected:
                flow_engine._validate_review_decision(json.loads(path.read_text()), {}, {}, decision)
            decision_validation.record_rejection(path, decision, rejected.value)
            return 'codex_decision_invalid'

        return worker.complete_review_result(action, reviewed, completer=validate, defer_invalid_stop=True)

    monkeypatch.setattr(probe, 'PROJECT_ROOT', tmp_path)
    result = probe._run_dispatched_tasks(
        2, 1, project_root=tmp_path, renew_actions=lambda paths: paths,
        review_worker=review, complete_review=complete, record_failure_round=probe._record_failure_round,
        quarantine_review=lambda *args: pytest.fail('rejected decision must stay pending'),
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {'status': 'healthy'})
    assert reviews == completions == actions
    assert writes == [actions[-1]]
    assert [outcome.value['status'] for outcome in result.outcomes] == ['retry_pending', 'completed']
    assert result.outcomes[0].value['process_attempts'] == 1
    assert result.outcomes[0].value['process_retries'] == 0
    state = json.loads(actions[0].with_name('state.json').read_text())
    assert state['execution_state'] == 'review_required' and state['production_failure_round'] == 1
    assert list(actions[0].parent.glob('review-rejection-*.json'))
    assert not actions[0].with_name('execution-receipt.json').exists()
    assert not runtime_control.stop_requested()
    assert guard.cooldown() is None


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
    identity = worker._request_review_stop(first, 'codex_review_parent_lost')
    assert identity is not None
    assert worker._request_review_stop(second, 'codex_review_parent_lost') is None
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
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    original_summary = worker.process_diagnostics.stderr_summary

    def delayed_summary(stream, **kwargs):
        if 'on_chunk' in kwargs:
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
    assert worker.process_diagnostics.read_notices(action)['first_official_notice']['classification'] == 'usage_limit'


def test_late_quota_classification_after_generic_stdout_failure_publishes_cooldown(tmp_path, monkeypatch):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    original_summary = worker.process_diagnostics.stderr_summary
    def delayed(stream, **kwargs):
        if 'on_chunk' in kwargs:
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
    notices = worker.process_diagnostics.read_notices(action)
    assert notices['first_blocking_notice']['classification'] == 'unknown_error'
    assert notices['first_official_notice']['classification'] == 'usage_limit'


def test_blocking_stdout_notice_keeps_later_official_error_from_same_read(tmp_path, monkeypatch):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c',
        'import sys,json; sys.stdin.read(); '
        'print(json.dumps({"type":"error","message":"generic failure"}),flush=True); '
        'print(json.dumps({"type":"error","codexErrorInfo":"usageLimitExceeded"}),flush=True)'])
    result = worker.review(task)
    assert result['error_code'] == 'codex_process_failed'
    assert guard.cooldown()['notice_code'] == 'usage_limit'
    notices = worker.process_diagnostics.read_notices(action)
    assert notices['first_blocking_notice']['classification'] == 'unknown_error'
    assert notices['first_official_notice']['classification'] == 'usage_limit'


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
    task, action = _small_marketing_v2_task(tmp_path, budget_account='宣传制作费')
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
    assert worker.process_diagnostics.read_notices(action) == {}


@pytest.mark.parametrize('info', [None, {'httpConnectionFailed': {'httpStatusCode': 502}}])
@pytest.mark.parametrize('channel', ['stdout', 'stderr', 'split_stderr'])
def test_explicit_reconnect_warning_can_finish_same_review(tmp_path, monkeypatch, info, channel):
    task, action = _small_marketing_v2_task(tmp_path, budget_account='宣传制作费')
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))

    def command(root, schema, decision, **kwargs):
        warning = {'type': 'error', 'message': 'Reconnecting... 1/5 (stream disconnected before completion)'}
        if info is not None:
            warning['codexErrorInfo'] = info
        text = json.dumps(warning)
        emit = f'print({text!r}, file=sys.{channel}, flush=True); ' if channel != 'split_stderr' else (
            f'sys.stderr.write({text[:40]!r}); sys.stderr.flush(); time.sleep(.05); '
            f'sys.stderr.write({text[40:]!r}); sys.stderr.flush(); ')
        return [sys.executable, '-c',
                'import sys,json,time; from pathlib import Path; sys.stdin.read(); ' + emit +
                f'Path({str(decision)!r}).write_text(json.dumps({{"overall":"pass"}})); '
                'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}), flush=True)']

    monkeypatch.setattr(worker, '_codex_command', command)
    result = worker.review(task)
    assert result['status'] == 'reviewed'
    assert result['process_attempts'] == 1
    assert result['process_retries'] == 0
    assert not runtime_control.stop_requested()
    assert guard.cooldown() is None
    notice = worker.process_diagnostics.read_notices(action)['first_notice']
    assert notice['classification'] == 'network_reconnect'
    assert notice['source'] == ('stdout' if channel == 'stdout' else 'stderr')


@pytest.mark.parametrize('fields', [
    {},
    {'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 502}}},
    {'code': 'httpConnectionFailed', 'httpStatusCode': 502},
    {'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 502}}, 'code': 'httpConnectionFailed', 'httpStatusCode': 500},
])
@pytest.mark.parametrize('channel', ['stdout', 'stderr', 'split_stderr', 'plain_stderr'])
def test_native_transport_fallback_can_finish_same_review(tmp_path, monkeypatch, fields, channel):
    task, action = _small_marketing_v2_task(tmp_path, budget_account='宣传制作费')
    message = _TRANSPORT_FALLBACK_WARNING
    launches = []

    def command(root, schema, decision, **kwargs):
        launches.append(decision)
        item = {'type': 'error', 'message': message, **fields}
        warning = message if channel == 'plain_stderr' else json.dumps({'type': 'item.completed', 'item': item})
        if channel == 'split_stderr':
            emit = (f'sys.stderr.write({warning[:40]!r}); sys.stderr.flush(); time.sleep(.05); '
                    f'sys.stderr.write({warning[40:]!r}); sys.stderr.flush(); ')
        else:
            stream = 'stdout' if channel == 'stdout' else 'stderr'
            emit = f'print({warning!r}, file=sys.{stream}, flush=True); '
        return [sys.executable, '-c',
                'import sys,json,time; from pathlib import Path; sys.stdin.read(); ' + emit +
                f'Path({str(decision)!r}).write_text(json.dumps({{"overall":"pass"}})); '
                'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}), flush=True)']

    monkeypatch.setattr(worker, '_codex_command', command)
    result = worker.review(task)
    assert result['status'] == 'reviewed'
    assert result['process_attempts'] == 1
    assert result['process_retries'] == 0
    assert len(launches) == 1
    assert not runtime_control.stop_requested()
    assert guard.cooldown() is None
    notices = worker.process_diagnostics.read_notices(action)
    assert set(notices) == {'first_notice'}
    notice = notices['first_notice']
    assert notice['classification'] == 'network_reconnect'
    assert notice['blocking'] is False
    assert notice['source'] == ('stdout' if channel == 'stdout' else 'stderr')
    assert notice['native_event_type'] == (None if channel == 'plain_stderr' else 'item.completed')
    assert notice['error_message'].startswith('Falling back from WebSockets to HTTPS transport.')
    assert 'Broken pipe' in notice['error_message']
    assert notice['error_message_sha256'] == hashlib.sha256(message.encode()).hexdigest()


@pytest.mark.parametrize('restriction,classification', [
    ({'message': 'usage limit exceeded'}, 'usage_limit'),
    ({'message': 'model is not available'}, 'model_unavailable'),
    ({'message': 'authentication failed'}, 'authentication'),
    ({'message': 'too many requests'}, 'rate_limit'),
    ({'message': 'server overloaded'}, 'server_overloaded'),
    ({'code': 'usageLimitExceeded', 'codexErrorInfo': 'httpConnectionFailed'}, 'usage_limit'),
    ({'code': 'modelUnavailable'}, 'model_unavailable'),
    ({'code': 'unauthorized'}, 'authentication'),
    ({'code': 'rateLimitExceeded'}, 'rate_limit'),
    ({'code': 'serverOverloaded'}, 'server_overloaded'),
    ({'httpStatusCode': 401}, 'authentication'),
    ({'httpStatusCode': 403}, 'authentication'),
    ({'httpStatusCode': 429, 'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 502}}}, 'rate_limit'),
    ({'httpStatusCode': 429, 'codexErrorInfo': {'responseStreamDisconnected': {'httpStatusCode': 401}}}, 'rate_limit'),
    ({'httpStatusCode': 503}, 'server_overloaded'),
])
def test_official_error_after_transport_fallback_stops_before_retry(tmp_path, monkeypatch, restriction, classification):
    task, action = _task(tmp_path)
    retry = tmp_path / 'retry-started'
    child_pid = tmp_path / 'model-pid'
    launches = []
    errors = [{'type': 'error', 'message': _TRANSPORT_FALLBACK_WARNING},
              {'type': 'error', **restriction,
               'message': _TRANSPORT_FALLBACK_WARNING + ' ' + restriction.get('message', '')}]

    def command(*args, **kwargs):
        launches.append(True)
        emit = ''.join(f'print({json.dumps({"type": "item.completed", "item": error})!r}, flush=True); '
                       for error in errors)
        script = ('import os,sys,time; from pathlib import Path; sys.stdin.read(); '
                  f'Path({str(child_pid)!r}).write_text(str(os.getpid())); ' + emit +
                  f'time.sleep(10); Path({str(retry)!r}).write_text("retry"); time.sleep(10)')
        return [sys.executable, '-c', script]

    monkeypatch.setattr(worker, '_codex_command', command)
    started = time.monotonic()
    result = worker.review(task)
    assert time.monotonic() - started < 8
    assert worker.OBSERVATION_INTERVAL_SECONDS == 5
    assert result['error_code'] == 'codex_process_failed'
    assert result['process_attempts'] == 1
    assert result['process_retries'] == 0
    assert len(launches) == 1
    assert not retry.exists()
    assert not action.with_name('execution-receipt.json').exists()
    assert runtime_control.stop_reason() == guard.STOP_REASON
    cooldown = guard.cooldown()
    assert cooldown['notice_code'] == classification
    assert cooldown['blocked_until'] - cooldown['triggered_at'] == 10800
    notices = worker.process_diagnostics.read_notices(action)
    assert notices['first_notice']['classification'] == 'network_reconnect'
    assert notices['first_notice']['blocking'] is False
    assert notices['first_blocking_notice']['classification'] == classification
    assert notices['first_official_notice']['classification'] == classification
    official = notices['first_official_notice']
    if 'code' in restriction:
        assert official['native_error_code'] == restriction['code']
        assert official['native_error_code_status'] == 'retained'
    if 'httpStatusCode' in restriction:
        assert official['http_status'] == restriction['httpStatusCode']
        assert official['http_status_source'] == 'httpStatusCode'
    with pytest.raises(ProcessLookupError):
        os.kill(int(child_pid.read_text()), 0)


@pytest.mark.parametrize('event,classification', [
    ({'type': 'turn.failed', 'error': {'message': _TRANSPORT_FALLBACK_WARNING}}, 'terminal_failure'),
    ({'type': 'thread.failed', 'error': {'message': _TRANSPORT_FALLBACK_WARNING}}, 'terminal_failure'),
    ({'type': 'item.completed', 'item': {'type': 'error', 'message': _TRANSPORT_FALLBACK_WARNING,
                                      'codexErrorInfo': 'unrecognizedCode'}}, 'unknown_error'),
    ({'type': 'error', 'message': 'Not falling back from WebSockets to HTTPS transport. stream disconnected'}, 'unknown_error'),
    ({'type': 'error', 'message': 'Falling back from WebSockets to HTTP transport. stream disconnected'}, 'unknown_error'),
    ({'type': 'error', 'message': 'Falling back from WebSockets to HTTPS transport. connection failed; will not retry'}, 'unknown_error'),
    ({'type': 'error', 'message': 'Falling back from WebSockets to HTTPS transport. '}, 'unknown_error'),
])
def test_transport_fallback_does_not_exempt_other_failures(tmp_path, monkeypatch, event, classification):
    task, action = _task(tmp_path)
    script = f'import sys; sys.stdin.read(); print({json.dumps(event)!r}, flush=True)'
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    result = worker.review(task)
    assert result['error_code'] == 'codex_process_failed'
    assert result['process_attempts'] == 1
    assert result['process_retries'] == 0
    assert not runtime_control.stop_requested()
    assert guard.cooldown() is None
    notice = worker.process_diagnostics.read_notices(action)['first_blocking_notice']
    assert notice['classification'] == classification
    assert notice['blocking'] is True


@pytest.mark.parametrize('event,classification', [
    ({'type': 'error', 'message': _TRANSPORT_FALLBACK_WARNING, 'code': 'futureNativeError'}, 'unknown_error'),
    ({'type': 'error', 'message': _TRANSPORT_FALLBACK_WARNING, 'code': 'futureNativeError',
      'codexErrorInfo': 'httpConnectionFailed'}, 'unknown_error'),
    ({'type': 'error', 'message': _TRANSPORT_FALLBACK_WARNING, 'code': None}, 'unknown_error'),
    ({'type': 'error', 'message': _TRANSPORT_FALLBACK_WARNING, 'code': [], 'httpStatusCode': 502}, 'unknown_error'),
    ({'type': 'error', 'message': _TRANSPORT_FALLBACK_WARNING, 'httpStatusCode': 418,
      'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 502}}}, 'unknown_error'),
    ({'type': 'future.event', 'item': {'type': 'error', 'message': _TRANSPORT_FALLBACK_WARNING}}, 'invalid_event'),
    ({'item': {'type': 'error', 'message': _TRANSPORT_FALLBACK_WARNING}}, 'invalid_event'),
    ({'type': [], 'item': {'type': 'error', 'message': _TRANSPORT_FALLBACK_WARNING}}, 'invalid_event'),
])
@pytest.mark.parametrize('channel', ['stdout', 'stderr'])
def test_transport_fallback_cannot_hide_unknown_fields_before_valid_completion(tmp_path, monkeypatch, event, classification, channel):
    task, action = _task(tmp_path)
    launches = []

    def command(root, schema, decision, **kwargs):
        launches.append(decision)
        return [sys.executable, '-c',
                'import sys,json; from pathlib import Path; sys.stdin.read(); '
                f'print({json.dumps(event)!r},file=sys.{channel},flush=True); '
                f'Path({str(decision)!r}).write_text(json.dumps({{"overall":"pass"}})); '
                'print(json.dumps({"type":"turn.completed"}),flush=True)']

    monkeypatch.setattr(worker, '_codex_command', command)
    result = worker.review(task)
    assert result['status'] == 'failed'
    assert result['process_attempts'] == 1
    assert result['process_retries'] == 0
    assert len(launches) == 1
    assert not runtime_control.stop_requested()
    assert guard.cooldown() is None
    assert not action.with_name('execution-receipt.json').exists()
    notice = worker.process_diagnostics.read_notices(action)['first_blocking_notice']
    assert notice['classification'] == classification
    assert notice['blocking'] is True


@pytest.mark.parametrize('complete,decision_present,exit_code,expected', [
    (False, True, 0, 'codex_process_incomplete'),
    (True, True, 1, 'codex_process_failed'),
    (True, False, 0, 'codex_process_incomplete'),
])
def test_transport_fallback_still_requires_successful_terminal_event(tmp_path, monkeypatch, complete, decision_present, exit_code, expected):
    task, action = _task(tmp_path)

    def command(root, schema, decision, **kwargs):
        warning = json.dumps({'type': 'item.completed', 'item': {'type': 'error', 'message': _TRANSPORT_FALLBACK_WARNING}})
        terminal = 'print(json.dumps({"type":"turn.completed"}),flush=True); ' if complete else ''
        write_decision = f'Path({str(decision)!r}).write_text(json.dumps({{"overall":"pass"}})); ' if decision_present else ''
        return [sys.executable, '-c',
                'import sys,json; from pathlib import Path; sys.stdin.read(); '
                f'print({warning!r},flush=True); ' + write_decision + terminal +
                f'sys.exit({exit_code})']

    monkeypatch.setattr(worker, '_codex_command', command)
    result = worker.review(task)
    assert result['error_code'] == expected
    assert result['process_attempts'] == 1
    assert result['process_retries'] == 0
    assert not runtime_control.stop_requested()
    assert guard.cooldown() is None
    assert set(worker.process_diagnostics.read_notices(action)) == {'first_notice'}


def test_reconnect_does_not_hide_first_official_blocking_notice(tmp_path, monkeypatch):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    monkeypatch.setenv(guard.RUN_ENV, 'a' * 32)
    monkeypatch.setenv(guard.INSTANCE_ENV, 'b' * 32)
    events = [
        {'type': 'error', 'message': 'Reconnecting... 1/5 (stream disconnected private-secret)'},
        {'type': 'error', 'message': 'Reconnecting... 2/5 (status 429 private-secret)',
         'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 429, 'url': 'https://private-secret'}},
         'token': 'private-secret'},
        {'type': 'error', 'codexErrorInfo': 'usageLimitExceeded'},
    ]
    script = 'import sys; sys.stdin.read(); ' + '; '.join(f'print({json.dumps(e)!r}, flush=True)' for e in events)
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    result = worker.review(task)
    assert result['error_code'] == 'codex_process_failed'
    notices = worker.process_diagnostics.read_notices(action)
    first = notices['first_notice']
    blocking = notices['first_blocking_notice']
    assert first['classification'] == 'network_reconnect'
    assert first['blocking'] is False
    assert blocking['classification'] == 'rate_limit'
    assert blocking['native_error_code'] == 'httpConnectionFailed'
    assert blocking['http_status'] == 429
    assert blocking['blocking'] is True
    assert first['action_sha256'] == blocking['action_sha256']
    assert first['launch_id_sha256'] == blocking['launch_id_sha256']
    assert first['run_id_sha256'] is not None and first['controller_instance_id_sha256'] is not None
    assert blocking['observed_at'] >= first['observed_at']
    assert guard.cooldown()['notice_code'] == 'rate_limit'
    diagnostic = json.loads(next(action.parent.glob('review-process-*.json')).read_text())
    assert diagnostic['notice_timing']['stop_requested_at'] >= blocking['observed_at']
    assert diagnostic['notice_timing']['process_exit_observed_at'] >= diagnostic['notice_timing']['stop_requested_at']
    assert all('private-secret' not in path.read_text() for path in action.parent.glob('review-notice-*.json'))


@pytest.mark.parametrize('channel', ['stdout', 'stderr', 'split_stderr'])
def test_unknown_native_error_retains_redacted_details_through_review(tmp_path, monkeypatch, channel):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    message = ('stream disconnected before completion: unexpected HTTP status 502 Bad Gateway; '
               'Authorization: Bearer private-secret; url=https://private.internal/path?token=private-query; '
               'account=private-account; invoice="private-business"')
    event = {'type': 'item.completed', 'item': {'type': 'error', 'message': message,
             'codexErrorInfo': {'responseStreamDisconnected': {'httpStatusCode': 502}}}}
    text = json.dumps(event)
    emit = f'print({text!r}, file=sys.{channel}, flush=True)' if channel != 'split_stderr' else (
        f'sys.stderr.write({text[:40]!r}); sys.stderr.flush(); time.sleep(.05); '
        f'sys.stderr.write({text[40:]!r}); sys.stderr.flush()')
    script = 'import sys,time; sys.stdin.read(); ' + emit
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    result = worker.review(task)
    assert result['status'] == 'failed' and result['process_retries'] == 0
    assert guard.cooldown() is None
    notice = worker.process_diagnostics.read_notices(action)['first_blocking_notice']
    assert notice['schema_version'] == 2
    assert notice['source'] == ('stdout' if channel == 'stdout' else 'stderr')
    assert notice['classification'] == 'unknown_error'
    assert notice['native_error_code'] == 'responseStreamDisconnected'
    assert notice['native_error_code_status'] == 'retained'
    assert notice['http_status'] == 502
    assert notice['http_status_source'] == 'codexErrorInfo'
    assert 'stream disconnected before completion' in notice['error_message']
    assert 'Bad Gateway' in notice['error_message']
    assert 'private' not in json.dumps(notice)
    assert notice['error_message_sha256'] == hashlib.sha256(message.encode()).hexdigest()
    assert notice['error_message_bytes'] == len(message.encode())
    assert notice['error_message_redacted'] is True
    assert notice['error_message_truncated'] is False
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in action.parent.glob('review-notice-*.json'))


@pytest.mark.parametrize('error,code,code_status,status,status_source', [
    ({'message': 'unexpected HTTP/1.1 502 Bad Gateway'}, None, 'missing', 502, 'message'),
    ({'message': 'unexpected status code: 504 upstream timeout'}, None, 'missing', 504, 'message'),
    ({'message': 'request failed', 'code': 'futureNativeError', 'httpStatusCode': 500},
     'futureNativeError', 'retained', 500, 'httpStatusCode'),
    ({'message': 'private invoice amount 502'}, None, 'missing', None, 'missing'),
    ({'message': 'connection failed url=https://private/status502'}, None, 'missing', None, 'missing'),
    ({'message': 'client error: request_id="status 502"'}, None, 'missing', None, 'missing'),
    ({'message': 'connection failed; account=status 502'}, None, 'missing', None, 'missing'),
    ({'message': 'connection failed; Authorization: status 504'}, None, 'missing', None, 'missing'),
    ({'message': 'connection failed endpoint /status 502'}, None, 'missing', None, 'missing'),
    ({'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 'bad'}}, 'httpStatusCode': 504},
     'httpConnectionFailed', 'retained', 504, 'httpStatusCode'),
    ({'codexErrorInfo': ['private-secret'], 'httpStatusCode': True},
     None, 'omitted_untrusted', None, 'omitted_untrusted'),
    ({'codexErrorInfo': 'sk-private-secret', 'message': 42}, None, 'omitted_untrusted', None, 'missing'),
])
def test_review_preserves_error_metadata_sources_without_inventing_http_status(
        tmp_path, monkeypatch, error, code, code_status, status, status_source):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    event = {'type': 'turn.failed', 'error': error}
    script = 'import sys; sys.stdin.read(); ' + f'print({json.dumps(event)!r}, flush=True)'
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    assert worker.review(task)['error_code'] == 'codex_process_failed'
    notice = worker.process_diagnostics.read_notices(action)['first_blocking_notice']
    assert notice['native_error_code'] == code
    assert notice['native_error_code_status'] == code_status
    assert notice['http_status'] == status
    assert notice['http_status_source'] == status_source
    assert 'private' not in json.dumps(notice)
    if not isinstance(error.get('message'), str):
        assert notice['error_message'] is None
        assert notice['error_message_sha256'] is None
        assert notice['error_message_bytes'] is None


@pytest.mark.parametrize('message,private_literal', [
    ('connection failed endpoint api.internal', 'api.internal'),
    ('permission denied: user/session', 'user/session'),
    ('permission denied: user\\session', 'user\\session'),
    ('connection failed endpoint a::a', 'a::a'),
])
def test_review_redacts_unqualified_addresses_and_relative_paths(tmp_path, monkeypatch, message, private_literal):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    event = {'type': 'error', 'message': message}
    script = 'import sys; sys.stdin.read(); ' + f'print({json.dumps(event)!r}, flush=True)'
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    assert worker.review(task)['error_code'] == 'codex_process_failed'
    notice = worker.process_diagnostics.read_notices(action)['first_blocking_notice']
    assert private_literal not in notice['error_message']
    assert '[redacted]' in notice['error_message']
    assert notice['error_message_redacted'] is True


@pytest.mark.parametrize('message', ['connection failed ' * 200, 'connection failed ' * 1000],
                         ids=['truncated', 'oversized'])
def test_review_bounds_long_error_messages_without_losing_notice(tmp_path, monkeypatch, message):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    event = {'type': 'error', 'message': message}
    script = 'import sys; sys.stdin.read(); ' + f'print({json.dumps(event)!r}, flush=True)'
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    assert worker.review(task)['error_code'] == 'codex_process_failed'
    notice = worker.process_diagnostics.read_notices(action)['first_blocking_notice']
    assert len(notice['error_message']) <= 2048
    assert notice['error_message_truncated'] is True
    assert notice['error_message_bytes'] == len(message.encode())
    assert notice['error_message_sha256'] == hashlib.sha256(message.encode()).hexdigest()


def test_schema_one_notice_remains_readable_without_rewriting_history(tmp_path, monkeypatch):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    event = {'type': 'error', 'codexErrorInfo': 'usageLimitExceeded'}
    script = 'import sys; sys.stdin.read(); ' + f'print({json.dumps(event)!r}, flush=True)'
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    assert worker.review(task)['error_code'] == 'codex_process_failed'
    legacy_fields = {'schema_version', 'kind', 'role', 'run_id_sha256', 'controller_instance_id_sha256',
                     'launch_id_sha256', 'action_sha256', 'classification', 'blocking', 'source',
                     'native_event_type', 'native_error_code', 'http_status', 'observed_at'}
    saved = {}
    for path in action.parent.glob('review-notice-*.json'):
        payload = {key: value for key, value in json.loads(path.read_text()).items() if key in legacy_fields}
        payload['schema_version'] = 1
        raw = json.dumps(payload, sort_keys=True).encode()
        target = path.with_name(f"review-notice-{payload['launch_id_sha256']}-{payload['role']}-{hashlib.sha256(raw).hexdigest()}.json")
        path.unlink()
        target.write_bytes(raw)
        saved[target] = raw
    notices = worker.process_diagnostics.read_notices(action)
    assert notices['first_official_notice']['schema_version'] == 1
    assert notices['first_official_notice']['native_error_code'] == 'usageLimitExceeded'
    assert all(path.read_bytes() == raw for path, raw in saved.items())


@pytest.mark.parametrize('field,value', [
    ('native_error_code_status', []), ('http_status_source', {}), ('http_status', True),
    ('error_message', 'https://private-secret/path'), ('error_message_sha256', []),
    ('error_message_redacted', 1), ('error_message_truncated', 'private-secret'),
])
def test_invalid_v2_error_evidence_is_rejected_even_when_rehashed(tmp_path, monkeypatch, field, value):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    event = {'type': 'error', 'message': 'stream disconnected before completion'}
    script = 'import sys; sys.stdin.read(); ' + f'print({json.dumps(event)!r}, flush=True)'
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    assert worker.review(task)['error_code'] == 'codex_process_failed'
    path = next(action.parent.glob('review-notice-*-first_blocking_notice-*.json'))
    payload = json.loads(path.read_text())
    payload[field] = value
    raw = json.dumps(payload, sort_keys=True).encode()
    target = path.with_name(f"review-notice-{payload['launch_id_sha256']}-{payload['role']}-{hashlib.sha256(raw).hexdigest()}.json")
    path.unlink()
    target.write_bytes(raw)
    target.chmod(0o600)
    with pytest.raises(ValueError, match='process_diagnostic_invalid'):
        worker.process_diagnostics.read_notices(action)
    assert target.read_bytes() == raw


def test_v2_error_evidence_rejects_public_permissions_without_rewriting(tmp_path, monkeypatch):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    event = {'type': 'error', 'message': 'connection failed'}
    script = 'import sys; sys.stdin.read(); ' + f'print({json.dumps(event)!r}, flush=True)'
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    assert worker.review(task)['error_code'] == 'codex_process_failed'
    path = next(action.parent.glob('review-notice-*-first_blocking_notice-*.json'))
    original = path.read_bytes()
    path.chmod(0o644)
    with pytest.raises(ValueError, match='process_diagnostic_invalid'):
        worker.process_diagnostics.read_notices(action)
    assert path.read_bytes() == original


@pytest.mark.parametrize('message,classification', [
    ('Reconnecting... connection failed: too many requests. Will retry in 10s', 'rate_limit'),
    ('Reconnecting... connection failed: server overloaded. Will retry in 10s', 'server_overloaded'),
    ('Reconnecting... connection failed: account suspended. Will retry in 10s', 'authentication'),
    ('Reconnecting... connection failed: authentication failed. Will retry in 10s', 'authentication'),
])
def test_first_stderr_restriction_blocks_before_cli_retry(tmp_path, monkeypatch, message, classification):
    task, action = _task(tmp_path)
    retry = tmp_path / 'retry'
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    script = ('import sys,time; from pathlib import Path; sys.stdin.read(); '
              f'sys.stderr.write({message[:20]!r}); sys.stderr.flush(); time.sleep(.05); '
              f'sys.stderr.write({message[20:]!r}); sys.stderr.flush(); '
              f'time.sleep(10); Path({str(retry)!r}).write_text("retry"); time.sleep(10)')
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    started = time.monotonic()
    result = worker.review(task)
    assert time.monotonic() - started < 8
    assert result['error_code'] == 'codex_process_failed'
    assert not retry.exists()
    assert guard.cooldown()['notice_code'] == classification
    notices = worker.process_diagnostics.read_notices(action)
    assert notices['first_blocking_notice']['classification'] == classification
    assert notices['first_blocking_notice']['source'] == 'stderr'
    assert notices['first_blocking_notice']['native_event_type'] is None


def test_decision_after_reconnect_without_terminal_event_is_not_success(tmp_path, monkeypatch):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))

    def command(root, schema, decision, **kwargs):
        return [sys.executable, '-c',
                'import sys,json; from pathlib import Path; sys.stdin.read(); '
                'print(json.dumps({"type":"error","message":"Reconnecting... 1/5 (stream disconnected)"}), flush=True); '
                f'Path({str(decision)!r}).write_text(json.dumps({{"overall":"pass"}}))']

    monkeypatch.setattr(worker, '_codex_command', command)
    result = worker.review(task)
    assert result['status'] == 'failed'
    assert result['error_code'] == 'codex_process_incomplete'
    assert result['process_retries'] == 0
    assert not runtime_control.stop_requested()
    diagnostic = json.loads(next(action.parent.glob('review-process-*.json')).read_text())
    timing = diagnostic['notice_timing']
    assert timing['stop_requested_at'] >= timing['process_exit_observed_at']


@pytest.mark.parametrize('failure,classification', [
    (json.dumps({'type': 'error', 'message': 'stream disconnected'}), 'unknown_error'),
    (json.dumps({'type': 'error', 'message': 'connection failed; not retrying'}), 'unknown_error'),
    (json.dumps({'type': 'error', 'message': 'Reconnecting stream disconnected', 'codexErrorInfo': 'private-code'}), 'unknown_error'),
    (json.dumps({'type': 'turn.failed', 'error': {'message': 'Reconnecting stream disconnected'}}), 'terminal_failure'),
    (json.dumps({'type': 'thread.failed', 'error': {'message': 'Reconnecting stream disconnected'}}), 'terminal_failure'),
    ('{broken private-secret', 'invalid_event'),
    (json.dumps({'type': 'private-secret'}), 'invalid_event'),
    (json.dumps({'type': []}), 'invalid_event'),
])
def test_reconnect_never_exempts_terminal_unknown_or_corrupt_events(tmp_path, monkeypatch, failure, classification):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    warning = json.dumps({'type': 'error', 'message': 'Reconnecting stream disconnected'})
    script = ('import sys; sys.stdin.read(); '
              f'print({warning!r}, flush=True); print({failure!r}, flush=True); '
              'print(\'{"type":"turn.completed"}\', flush=True)')
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    result = worker.review(task)
    assert result['status'] == 'failed'
    assert result['error_code'] == 'codex_process_failed'
    notices = worker.process_diagnostics.read_notices(action)
    assert notices['first_notice']['classification'] == 'network_reconnect'
    assert notices['first_blocking_notice']['classification'] == classification
    if classification == 'invalid_event':
        assert notices['first_blocking_notice']['native_event_type'] is None
    assert guard.cooldown() is None
    assert result['process_attempts'] == 1 and result['process_retries'] == 0


def test_original_notice_readback_preserves_earlier_launches(tmp_path, monkeypatch):
    task, action = _small_marketing_v2_task(tmp_path, budget_account='宣传制作费')
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))

    def command(root, schema, decision, **kwargs):
        return [sys.executable, '-c',
                'import sys,json; from pathlib import Path; sys.stdin.read(); '
                'print(json.dumps({"type":"error","message":"Reconnecting stream disconnected"}), flush=True); '
                f'Path({str(decision)!r}).write_text(json.dumps({{"overall":"pass"}})); '
                'print(\'{"type":"turn.completed"}\', flush=True)']

    monkeypatch.setattr(worker, '_codex_command', command)
    assert worker.review(task)['status'] == 'reviewed'
    original = worker.process_diagnostics.read_notices(action)
    launch = original['first_notice']['launch_id_sha256']
    original_files = {path: path.read_bytes() for path in action.parent.glob('review-notice-*.json')}
    assert worker.review(task)['status'] == 'reviewed'
    assert worker.process_diagnostics.read_notices(action, launch_id_sha256=launch) == original
    assert all(path.read_bytes() == raw for path, raw in original_files.items())
    archived_action = action.with_name('interrupted-action.json')
    archived_action.write_bytes(action.read_bytes())
    changed = json.loads(action.read_text())
    changed['write_authorized'] = False
    changed['action'] = 'needs_admin'
    action.write_text(json.dumps(changed))
    assert worker.process_diagnostics.read_notices(archived_action, launch_id_sha256=launch) == original


@pytest.mark.parametrize('damage', ['content', 'symlink', 'rehashed'])
def test_tampered_first_notice_blocks_without_overwriting_evidence(tmp_path, monkeypatch, damage):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    script = '''import hashlib,json,sys,time
from pathlib import Path
sys.stdin.read()
root = Path(ROOT)
print(json.dumps({"type":"error","message":"Reconnecting stream disconnected"}), flush=True)
deadline = time.monotonic() + 7
while not list(root.glob("review-notice-*.json")) and time.monotonic() < deadline:
    time.sleep(.01)
notice = next(root.glob("review-notice-*.json"))
original = notice.read_bytes()
if DAMAGE == "content":
    notice.write_text("{}")
elif DAMAGE == "symlink":
    saved = root / "original-notice.json"
    saved.write_bytes(original)
    notice.unlink()
    notice.symlink_to(saved)
else:
    payload = json.loads(original)
    payload["run_id_sha256"] = "c" * 64
    payload["controller_instance_id_sha256"] = "d" * 64
    digest = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    replacement = notice.with_name(notice.name.rsplit("-",1)[0] + "-" + digest + ".json")
    notice.unlink()
    replacement.write_text(json.dumps(payload))
if DAMAGE != "rehashed":
    print(json.dumps({"type":"error","message":"Reconnecting stream disconnected"}), flush=True)
print(json.dumps({"type":"turn.completed"}), flush=True)
'''.replace('ROOT', repr(str(action.parent))).replace('DAMAGE', repr(damage))
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    result = worker.review(task)
    assert result['error_code'] == 'codex_process_diagnostic_failed'
    assert runtime_control.stop_requested()
    notice = next(action.parent.glob('review-notice-*.json'))
    if damage == 'symlink':
        assert notice.is_symlink()
    elif damage == 'content':
        assert notice.read_text() == '{}'
    else:
        assert json.loads(notice.read_text())['run_id_sha256'] == 'c' * 64


def test_first_stderr_official_observation_blocks_another_review_immediately(tmp_path, monkeypatch):
    first_task, action = _task(tmp_path / 'first')
    next_task, _ = _task(tmp_path / 'next')
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c',
        'import sys,time; sys.stdin.read(); print("usage limit exceeded",file=sys.stderr,flush=True); time.sleep(10)'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(worker.review, first_task)
        deadline = time.monotonic() + 3
        while not list(action.parent.glob('review-notice-*.json')) and time.monotonic() < deadline:
            time.sleep(.01)
        assert list(action.parent.glob('review-notice-*.json'))
        result = worker.review(next_task)
        assert result['error_code'] == 'stop_requested'
        assert result['process_attempts'] == 0
        assert first.result(timeout=8)['error_code'] == 'codex_process_failed'


@pytest.mark.parametrize('exit_code,expected', [(0, 'codex_process_incomplete'), (1, 'codex_process_failed')])
def test_reconnect_with_missing_decision_or_nonzero_exit_remains_blocked(tmp_path, monkeypatch, exit_code, expected):
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker, '_reviewer_environment', lambda: dict(os.environ))
    script = ('import sys,json; sys.stdin.read(); '
              'print(json.dumps({"type":"error","message":"Reconnecting stream disconnected"}),flush=True); '
              'print(json.dumps({"type":"turn.completed"}),flush=True); '
              f'sys.exit({exit_code})')
    monkeypatch.setattr(worker, '_codex_command', lambda *a, **kw: [sys.executable, '-c', script])
    result = worker.review(task)
    assert result['status'] == 'failed'
    assert result['error_code'] == expected
    assert guard.cooldown() is None
    diagnostic = json.loads(next(action.parent.glob('review-process-*.json')).read_text())
    timing = diagnostic['notice_timing']
    assert timing['stop_requested_at'] >= timing['process_exit_observed_at']


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
