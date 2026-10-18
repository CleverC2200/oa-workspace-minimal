from concurrent.futures import ThreadPoolExecutor
import json
import time
from uuid import uuid4

import pytest

from apps.oa_finance_preaudit import host_chat_review as host, production_config
from scripts import finance_preaudit_concurrency_probe as controller
from tests.finance_preaudit.test_host_chat_review import exchange  # noqa: F401
from tests.finance_preaudit.test_concurrency_probe import _open_action


@pytest.mark.parametrize('provider,transport,seconds', [('openai', 'host_chat', 1800), ('deepseek', 'cli', 900)])
def test_new_round_review_budget_is_selected_without_changing_identity(monkeypatch, provider, transport, seconds):
    monkeypatch.setenv('OA_REVIEW_PROVIDER', provider)
    monkeypatch.setenv('OA_REVIEW_TRANSPORT', transport)
    identity = production_config.review_settings()
    assert production_config.review_timeout_seconds() == seconds
    assert production_config.review_settings() == identity


def test_public_dispatcher_uses_same_app_budget_for_request_and_watchdog(exchange, monkeypatch):
    action, draft, _, _, _, manifest, _ = exchange
    _open_action(action.parents[4], case_key='synthetic-budget', created_at='2026-10-14T00:00:00+00:00')
    tasks, deadlines = [], []
    original = host.approval_lifecycle.heartbeat

    def heartbeat(*args, **kwargs):
        deadlines.extend(deadline - time.time() for deadline in args[3])
        return original(*args, **kwargs)

    def review(task):
        tasks.append(task)
        time.sleep(.05)
        return {'status': 'reviewed', 'decision': json.loads(draft.read_text()), 'write_state': 'not_requested'}

    monkeypatch.setattr(host.approval_lifecycle, 'heartbeat', heartbeat)
    # Explicit in-process fake transport; native pool ownership has separate coverage.
    monkeypatch.setattr(host.approval_lifecycle, 'submit_readonly',
        lambda executor, function, task, **kwargs: executor.submit(function, task))
    result = controller._run_dispatched_tasks(1, 1, project_root=action.parents[6],
        renew_actions=lambda paths: paths, review_worker=review,
        prepare_action=lambda _: {'action': 'no_action'},
        complete_review=lambda *_: {'status': 'completed', 'write_state': 'submitted', 'business_state': 'needs_human'},
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {'status': 'healthy'}, heartbeat_interval_sec=.01,
        run_id=manifest['run_id'], controller_instance_id=manifest['controller_instance_id'])
    assert result.completed == 1 and tasks[0]['timeout_seconds'] == 1800
    assert any(1850 < delta <= 1860 for delta in deadlines)


@pytest.mark.parametrize('damage,code', [('expired', 'host_review_request_expired'), ('frozen_and_expired', 'host_review_request_untrusted')])
def test_exchange_preserves_bound_failure_without_extending_old_request(exchange, monkeypatch, damage, code):
    action, _, owner, *_ = exchange
    request = host.publish(action, prompt='Current frozen review.', timeout_seconds=60)
    raw = request.read_bytes()
    now = time.time()
    if damage == 'frozen_and_expired':
        material = json.loads(action.with_name('material.json').read_text())
        material['warnings'] = ['Changed input']
        host.guard.write(action.with_name('material.json'), material)
    monkeypatch.setattr(host.time, 'time', lambda: now + 61)
    result = host.await_result(request)
    assert result['status'] == 'failed' and result['error_code'] == 'host_review_exchange_failed'
    assert result['failure_detail_code'] == code and result['failure_recorded'] is True
    registry = json.loads(host.registry_file(owner).read_text())
    if damage == 'expired':
        request_id = json.loads(raw)['request_id']
        failure = registry['request_failures'][request_id]
        reference = host.failure_reference(action, result)
        assert reference is not None and host.verified_failure_reference(action, reference) == failure
        assert failure['failure_detail_code'] == code and failure['request_file'] == str(request)
        assert 'first_failure' not in registry and registry['state'] == 'open'
        assert host.runtime_control.stop_requested() is False
    else:
        failure = registry['first_failure']
        assert failure['failure_detail_code'] == code and failure['operation'] == 'await_result'
        assert failure['request_file'] == str(request) and failure['exception_type'] == 'ValueError'
        assert not registry.get('request_failures')
        assert host.runtime_control.stop_requested() is True
    assert request.read_bytes() == raw
    host.await_result(request)
    retained = json.loads(host.registry_file(owner).read_text())
    if damage == 'expired':
        assert retained['request_failures'][request_id] == failure and 'first_failure' not in retained
    else:
        assert retained['first_failure'] == failure
    assert request.read_bytes() == raw
    assert not host._submission_file(request).exists()


def test_exchange_does_not_save_arbitrary_exception_body(exchange, monkeypatch):
    action, _, owner, *_ = exchange
    request = host.publish(action, prompt='Current frozen review.', timeout_seconds=60)
    def broken(*args, **kwargs):
        raise RuntimeError('synthetic-private-response-body')
    monkeypatch.setattr(host, '_live', broken)
    result = host.await_result(request)
    stored = host.registry_file(owner).read_text()
    assert result['failure_detail_code'] == 'host_review_exception'
    assert 'synthetic-private-response-body' not in stored and 'synthetic-private-response-body' not in str(result)


def test_expiry_does_not_mask_registry_identity_conflict_or_certify_wrong_round(exchange, monkeypatch):
    action, _, owner, *_ = exchange
    request = host.publish(action, prompt='Current frozen review.', timeout_seconds=60)
    value = json.loads(host.registry_file(owner).read_text())
    value['run_id'] = uuid4().hex
    host.guard.write(host.registry_file(owner), value)
    now = time.time()
    monkeypatch.setattr(host.time, 'time', lambda: now + 61)
    result = host.await_result(request)
    assert result['failure_detail_code'] == 'host_review_registry_untrusted'
    assert result['failure_recorded'] is False
    assert 'first_failure' not in json.loads(host.registry_file(owner).read_text())


def test_failure_capture_error_stays_failed_and_reports_missing_evidence(exchange, monkeypatch):
    action, _, owner, *_ = exchange
    request = host.publish(action, prompt='Current frozen review.', timeout_seconds=60)
    now = time.time()
    monkeypatch.setattr(host.time, 'time', lambda: now + 61)
    original = host.guard.write
    def write(path, value):
        if path == host.registry_file(owner):
            raise OSError('synthetic-private-path')
        return original(path, value)
    monkeypatch.setattr(host.guard, 'write', write)
    result = host.await_result(request)
    assert result['status'] == 'failed' and result['failure_recorded'] is False
    assert result['failure_detail_code'] == 'host_review_request_expired'
    assert 'synthetic-private-path' not in str(result)
