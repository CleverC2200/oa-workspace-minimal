from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import json
import time
from uuid import uuid4

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import approval_guard as guard, approval_lifecycle as lifecycle
from apps.oa_finance_preaudit import controller_handoff, host_chat_review, review_interruption
from tests.finance_preaudit.test_approval_guard import prepared_case
from tests.finance_preaudit.test_host_chat_review import exchange as exchange


@contextmanager
def native_round(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_control, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    monkeypatch.setenv('OA_REVIEW_TRANSPORT', 'host_chat')
    monkeypatch.setenv(guard.OWNER_ENV, str(uuid4()))
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({'capacity': 3, 'review_configuration': host_chat_review.IDENTITY})
        with lifecycle.run_scope(manifest, tmp_path):
            yield manifest


def observe_notice(monkeypatch, manifest, code, turn, *, occurred_at=None):
    source = f"main:{manifest['owner_task_id']}:{turn}"
    stamp = time.time() if occurred_at is None else occurred_at
    monkeypatch.setattr(guard, 'project_official_errors', lambda _: iter([(code, source, stamp)]))
    guard.observe_project_errors(runtime_control.PROJECT_ROOT)
    return guard.retry_budget(manifest['run_id'])


@pytest.mark.parametrize('codes', [('rate_limit',) * 3, ('server_overloaded',) * 3,
                                 ('rate_limit', 'server_overloaded', 'rate_limit')])
def test_native_round_retries_first_two_notices_and_stops_third_without_cooldown(tmp_path, monkeypatch, codes):
    with native_round(tmp_path, monkeypatch) as manifest:
        for index, code in enumerate(codes, 1):
            receipt = observe_notice(monkeypatch, manifest, code, f'turn-{index}')
            assert receipt['failures'] == index
            assert receipt['retry_allowed'] is (index <= 2)
            assert runtime_control.stop_requested() is (index == 3)
            assert guard.cooldown() is None
        assert runtime_control.stop_reason() == guard.RETRY_STOP_REASON


def test_native_retry_budget_deduplicates_sources_and_ignores_older_events(tmp_path, monkeypatch):
    with native_round(tmp_path, monkeypatch) as manifest:
        first = observe_notice(monkeypatch, manifest, 'rate_limit', 'first')
        assert observe_notice(monkeypatch, manifest, 'rate_limit', 'first') == first
        observe_notice(monkeypatch, manifest, 'server_overloaded', 'historical', occurred_at=1)
        assert guard.retry_budget(manifest['run_id'])['failures'] == 1
        assert not runtime_control.stop_requested()


def test_native_budget_stop_revokes_unwritten_review_and_clears_only_after_reaping(tmp_path, monkeypatch):
    root, rows, collections, engine, action = prepared_case(tmp_path)
    original = action.read_bytes()
    with native_round(tmp_path, monkeypatch) as manifest:
        lifecycle.heartbeat(manifest['run_id'], manifest['controller_instance_id'], {action}, [], writer_active=False)
        for index in range(3):
            observe_notice(monkeypatch, manifest, 'server_overloaded', f'turn-{index}')
        assert lifecycle.interrupt_unwritten(manifest, cause=guard.RETRY_STOP_REASON) == 1
        state = json.loads(action.with_name('state.json').read_text())
        assert review_interruption.trusted(action.parent, state)
        assert action.with_name('interrupted-action.json').read_bytes() == original
        assert json.loads(action.with_name('review-interruption.json').read_text())['next_step'] == 'fresh_prepare_next_round'
        controller_handoff.finish(manifest, status='failed', children_reaped=True, health='degraded')
        lifecycle.clear_retry_stop(manifest)
        assert not runtime_control.stop_requested()
        assert guard.cooldown() is None
        next_action = engine.recover_confirmed_no_write(action, confirmed=True)
        assert next_action['attempt'] == 2 and len(collections) == 2


def test_native_retry_budget_does_not_exempt_authentication(tmp_path, monkeypatch):
    with native_round(tmp_path, monkeypatch) as manifest:
        observe_notice(monkeypatch, manifest, 'rate_limit', 'first')
        guard.record_notice('authentication', 'main:authentication:failure')
        assert runtime_control.stop_reason() == guard.STOP_REASON
        assert guard.cooldown()['notice_code'] == 'authentication'


@pytest.mark.parametrize('condition', ['expired', 'overridden', 'outside_window'])
def test_legacy_stop_never_restarts_or_extends_cooldown_for_new_transient_error(tmp_path, monkeypatch, condition):
    monkeypatch.setattr(runtime_control, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    monkeypatch.setenv('OA_REVIEW_TRANSPORT', 'host_chat')
    stamp = time.time()
    old_stamp = stamp - guard.COOLDOWN_SECONDS - 1 if condition == 'expired' else stamp
    previous = guard.record_notice('server_overloaded', 'legacy-official-error', occurred_at=old_stamp)
    runtime_control.request_stop(guard.STOP_REASON)
    if condition == 'overridden':
        monkeypatch.setattr(guard, 'cooldown_was_overridden', lambda _: True)
    event_stamp = previous['triggered_at'] - 1 if condition == 'outside_window' else stamp
    raw = (guard.directory() / 'cooldown.json').read_bytes()
    monkeypatch.setattr(guard, 'project_official_errors', lambda _: iter([
        ('rate_limit', f'main:{uuid4()}:new-foreign-error', event_stamp)]))
    guard.observe_project_errors(tmp_path)
    assert (guard.directory() / 'cooldown.json').read_bytes() == raw
    assert runtime_control.stop_reason() == guard.STOP_REASON


def test_parallel_native_observers_share_one_budget(tmp_path, monkeypatch):
    with native_round(tmp_path, monkeypatch) as manifest:
        events = [('rate_limit', f"main:{manifest['owner_task_id']}:turn-{index}", time.time()) for index in range(3)]
        monkeypatch.setattr(guard, 'project_official_errors', lambda _: iter(events))
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(guard.observe_project_errors, [tmp_path] * 3))
        assert guard.retry_budget(manifest['run_id'])['failures'] == 3
        assert runtime_control.stop_reason() == guard.RETRY_STOP_REASON
        assert guard.cooldown() is None


def test_native_observer_does_not_charge_another_rounds_reviewer(tmp_path, monkeypatch):
    with native_round(tmp_path, monkeypatch) as manifest:
        monkeypatch.setattr(guard, 'project_official_errors', lambda _: iter([
            ('rate_limit', f'main:{uuid4()}:old-reviewer', time.time())]))
        guard.observe_project_errors(tmp_path)
        assert guard.retry_budget(manifest['run_id']) is None
        assert not runtime_control.stop_requested() and guard.cooldown() is None


def test_native_retry_budget_rejects_changed_registered_reviewer_provenance(exchange, monkeypatch):
    _, _, owner, _, _, manifest, _ = exchange
    path = host_chat_review.registry_file(owner, manifest['run_id'])
    registry = json.loads(path.read_text())
    registry['reviewers'][0]['reviewer_task_id'] = str(uuid4())
    guard.write(path, registry)
    monkeypatch.setattr(guard, 'project_official_errors', lambda _: iter([
        ('rate_limit', f'main:{registry["reviewers"][0]["reviewer_task_id"]}:foreign-turn', time.time())]))
    with pytest.raises(ValueError, match='approval_official_history_unavailable'):
        guard.observe_project_errors(runtime_control.PROJECT_ROOT)
    assert guard.retry_budget(manifest['run_id']) is None
    assert not runtime_control.stop_requested() and guard.cooldown() is None


def test_native_retry_stop_does_not_clear_before_terminal_or_override_manual_stop(tmp_path, monkeypatch):
    with native_round(tmp_path, monkeypatch) as manifest:
        for index in range(3):
            observe_notice(monkeypatch, manifest, 'rate_limit', f'turn-{index}')
        with pytest.raises(ValueError, match='approval_retry_stop_untrusted'):
            lifecycle.clear_retry_stop(manifest)
        runtime_control.request_stop()
        assert runtime_control.stop_reason() is None and runtime_control.stop_requested()
        assert lifecycle.clear_retry_stop(manifest) is False


@pytest.mark.parametrize('error,expected', [
    ({'codexErrorInfo': 'rateLimitExceeded', 'httpStatusCode': 401}, 'authentication'),
    ({'codexErrorInfo': 'serverOverloaded', 'code': 'usageLimitExceeded'}, 'usage_limit'),
    ({'codexErrorInfo': 'rateLimitExceeded', 'code': 'modelUnavailable'}, 'model_unavailable'),
])
def test_transient_notice_never_hides_nonretryable_official_fields(error, expected):
    assert guard.notice_code(error) == expected


def test_controller_retains_third_notice_and_recovers_round_stop_without_cooldown(tmp_path, monkeypatch):
    from scripts import finance_preaudit_concurrency_probe as probe
    from tests.finance_preaudit.test_concurrency_probe import _reconciliation_snapshot

    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    monkeypatch.setenv('OA_REVIEW_TRANSPORT', 'host_chat')
    monkeypatch.setenv(guard.OWNER_ENV, str(uuid4()))
    monkeypatch.setattr(probe, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(runtime_control, 'PROJECT_ROOT', tmp_path)

    def pipeline(count, capacity, **kwargs):
        manifest = controller_handoff._read_manifest()
        for index in range(3):
            observe_notice(monkeypatch, manifest, 'server_overloaded', f'turn-{index}')
        return probe.PoolResult(requested=count, started=1, completed=0, failed=1, timed_out=0,
            stopped=count - 1, peak_active=1, elapsed_seconds=1,
            primary_error_code='production_probe_prepare_failed', outcomes=(
                probe.TaskOutcome(index=0, status='failed', value={'status': 'failed',
                    'error_code': 'production_probe_prepare_failed', 'token_usage': {'status': 'unknown'}}),))

    monkeypatch.setattr(probe, '_run_global_dispatched_tasks', pipeline)
    result = probe.run_rolling_drain(capacity=3, max_capacity=3, pipeline_runner=pipeline,
        live_status=lambda: {'credentials': {'status': 'ok'}, 'oa': {
            'execution_identity': 'verified', 'by_workflow': {'travel_expense': 2}}},
        reconcile_status=lambda: _reconciliation_snapshot(2))
    assert result['status'] == 'failed'
    assert result['primary_error_code'] == guard.RETRY_STOP_REASON
    assert result['transient_retries']['failures'] == 3
    assert result['controller_handoff']['children_reaped'] is True
    assert not runtime_control.stop_requested() and guard.cooldown() is None
