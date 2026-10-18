from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import time

import pytest

from apps import json_artifact
from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import (
    flow_engine, host_chat_review as host, item_failure, reconciliation,
    review_failure_evidence as evidence,
)
from tests.finance_preaudit.test_host_chat_review import exchange
from tests.finance_preaudit.test_host_chat_review import draft_source, native_call
from scripts import finance_preaudit_concurrency_probe as controller


@pytest.fixture(autouse=True)
def reject_live_preparation_credentials_and_alerts(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail('Live preparation, credentials and notifications are forbidden in host failure tests')

    monkeypatch.setattr(controller, '_preparation_executor', lambda: ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(controller, '_prepare_one', forbidden)
    monkeypatch.setattr(controller.cli.legacy_flow, 'load_config', forbidden)
    monkeypatch.setattr(controller.cli.legacy_flow, 'load_all_configs', forbidden)
    monkeypatch.setattr(controller.session_alert, 'alert_once', forbidden)
    monkeypatch.setattr(controller.session_alert, 'send_message', forbidden)



@pytest.fixture
def controlled_exchange(exchange):
    original, _, _, _, _, manifest, _ = exchange
    calls = []
    def grant(context):
        return {
            'schema_version': 1, 'kind': 'preaudit_write_grant', **context,
            'grant_id': 'offline-host-failure', 'mode': 'controlled_write',
            'expires_at': '2099-01-01T00:00:00+00:00', 'proof': 'offline-bound-grant',
        }
    engine = flow_engine.FlowEngine(
        root=original.parents[4],
        discover_candidates=lambda workflow: [{
            'workflow_key': workflow, 'request_id': 'offline-host-failure-' + workflow,
            'received_at': {
                'general_expense': '2026-10-10T09:00:00+08:00',
                'travel_expense': '2026-10-10T09:01:00+08:00',
                'business_entertainment': '2026-10-10T09:02:00+08:00',
            }[workflow],
        }],
        collect_material=lambda request, attempt: {
            'schema_version': 1, 'kind': 'preaudit_material', 'request_id': request,
            'workflow_key': attempt.parents[2].name, 'workflow_name': 'offline form',
            'declared_fields': [{'field_name': 'period', 'value': '2026-08'}],
            'resources': [], 'prepared': {'images': [], 'structured_contents': [], 'manifest': []},
            'authority_facts': [], 'warnings': [],
        },
        authorization_source=grant, authorization_verifier=lambda value, context: value == grant(context),
        submit_frozen=lambda job: calls.append(job),
    )
    action = Path(engine.next_action('general_expense')['action_file'])
    host.approval_lifecycle.heartbeat(manifest['run_id'], manifest['controller_instance_id'],
                                      {action}, [], writer_active=False)
    return engine, action, calls


def test_verified_host_expiry_becomes_an_item_local_quarantine(exchange, controlled_exchange, monkeypatch):
    engine, action, calls = controlled_exchange
    original = json.loads(action.read_text())
    packet = json.loads(action.with_name('review-packet.json').read_text())
    late_decision = {
        'schema_version': 1, 'kind': 'preaudit_review_decision',
        'action_token': original['action_token'], 'workflow_key': original['workflow_key'],
        'rule_version': packet['rule_version'], 'overall': 'needs_human',
        'items': [{'item_key': item['item_key'], 'status': 'uncertain',
                   'conclusion': '自动读取材料不足，需财务核对原单。',
                   'evidence_refs': [], 'missing_evidence': ['缺少审核材料']}
                  for item in packet['items']],
    }
    engine.validate_review_decision(action, late_decision)
    late_file = action.with_name('late-model-decision.json')
    json_artifact.write_json_atomic(late_file, late_decision)
    request = host.publish(action, prompt='Review current frozen input.', timeout_seconds=60)
    now = time.time()
    monkeypatch.setattr(host.time, 'time', lambda: now + 61)
    reviewed = host.await_result(request)
    reference = host.failure_reference(action, reviewed)
    assert reference is not None

    quarantined = engine.quarantine_review_failure(action, 'host_review_request_expired', host_failure_reference=reference)

    assert quarantined['action'] == 'needs_admin'
    assert quarantined['reason'] == 'codex_review_failed'
    assert quarantined['write_state'] == 'not_requested'
    assert quarantined['write_authorized'] is False
    state = json.loads(action.with_name('state.json').read_text())
    assert evidence.trusted_quarantine(action.parent, state)
    assert item_failure.isolated(action.parent, state)
    assert reconciliation.quarantine_summary(action.parents[4], pending_rows=[{
        'request_id': 'offline-host-failure-general_expense', 'received_at': '2026-10-10T09:00:00+08:00',
    }]) == {'local_quarantined': 1, 'pending_quarantined': 1, 'actionable_pending': 0}
    assert calls == []
    assert runtime_control.stop_requested() is False
    late_result = engine.complete_review(action, late_file)
    assert late_result['action'] == 'needs_admin'
    assert late_result['write_state'] == 'not_requested'
    assert calls == []
    assert runtime_control.stop_requested() is False
    host.close(owner=exchange[2], reason='cancelled')
    assert evidence.trusted_quarantine(action.parent, state)
    assert item_failure.isolated(action.parent, state)


class SimultaneousReviews:
    def __init__(self, capacity):
        self.capacity = capacity
        self.tasks = []

    def submit(self, function, task):
        future = Future()
        self.tasks.append((future, function, task))
        if len(self.tasks) == self.capacity:
            for pending, run, value in self.tasks:
                pending.set_result(run(value))
        return future

    def shutdown(self, **_kwargs):
        pass


@pytest.mark.parametrize('writer_unknown', [False, True])
def test_native_host_failure_keeps_same_tick_siblings_but_unknown_write_stops(
    exchange, controlled_exchange, monkeypatch, writer_unknown, request,
):
    engine, first, calls = controlled_exchange
    _, _, owner, first_child, _, manifest, _ = exchange
    actions = [first, *(Path(engine.next_action(workflow)['action_file'])
                        for workflow in ('travel_expense', 'business_entertainment'))]
    host.approval_lifecycle.heartbeat(manifest['run_id'], manifest['controller_instance_id'],
                                      set(actions), [], writer_active=False)
    requests = [host.publish(action, prompt='Review this frozen input only.',
                             timeout_seconds=60 if index == 0 else 300)
                for index, action in enumerate(actions)]
    reviewers = host.registered_reviewers(owner=owner)
    parent = first_child.with_name('parent.jsonl')
    native_reviewers = request.node.native_reviewers
    for index, (action, request) in enumerate(zip(actions, requests)):
        line = native_call(parent, 'followup_task', {
            'target': reviewers[index]['agent_path'], 'message': str(request),
        }, f'dispatch-{index}', {})
        host.assign(request, slot=index, owner=owner, source=parent, source_line=line)
        if index:
            packet = json.loads(action.with_name('review-packet.json').read_text())
            draft = action.with_name(f'offline-decision-{index}.json')
            child = Path(native_reviewers[index]['session_file'])
            line = draft_source(child, request, draft, owner, call_id=f'review-{index}', payload={
                'overall': 'needs_human', 'items': [{
                    'item_key': item['item_key'], 'status': 'uncertain',
                    'conclusion': '自动读取材料不足，需财务核对原单。',
                    'evidence_refs': [], 'missing_evidence': ['缺少审核材料'],
                } for item in packet['items']],
            })
            host.submit(request, draft, owner=owner, source=child, source_line=line)
    now = time.time()
    monkeypatch.setattr(host.time, 'time', lambda: now + 61)
    reviewed = {action: host.await_result(request) for action, request in zip(actions, requests)}
    monkeypatch.setattr(host.approval_lifecycle, 'submit_readonly',
                        lambda executor, function, *args, **kwargs: executor.submit(function, *args))
    monkeypatch.setattr(controller, '_preparation_executor', lambda: ThreadPoolExecutor(max_workers=1))
    prepared, writes = iter(actions), []
    def prepare(_selection):
        action = next(prepared, None)
        return {'action': 'review_required', 'action_file': str(action)} if action else {'action': 'no_action'}
    def complete(action, _reviewed):
        writes.append(action)
        if writer_unknown:
            return {'status': 'failed', 'write_state': 'unknown', 'error_code': 'writer_result_unknown'}
        return {'status': 'completed', 'business_state': 'needs_human', 'write_state': 'submitted', 'error_code': None}

    result = controller._run_dispatched_tasks(
        3, 3, project_root=first.parents[6], renew_actions=lambda paths: paths,
        prepare_action=prepare, review_worker=lambda task: reviewed[Path(task['action_file'])],
        complete_review=complete, quarantine_review=engine.quarantine_review_failure,
        executor_factory=SimultaneousReviews, strict_global_fifo=True,
        runtime_status=lambda: {'status': 'healthy'},
        run_id=manifest['run_id'], controller_instance_id=manifest['controller_instance_id'],
    )

    state = json.loads(first.with_name('state.json').read_text())
    assert state['action'] == 'needs_admin' and state['write_state'] == 'not_requested'
    assert state['write_authorized'] is False and item_failure.isolated(first.parent, state)
    assert writes == (actions[1:2] if writer_unknown else actions[1:])
    assert runtime_control.stop_requested() is writer_unknown
    assert result.outcomes[0].value['status'] == 'quarantined'
    if not writer_unknown:
        assert [outcome.value['status'] for outcome in result.outcomes] == ['quarantined', 'completed', 'completed']
        assert host.registered_reviewers(owner=owner) == reviewers
        summary = controller._rolling_wave_metrics(result)
        assert summary['failed'] == 0 and summary['isolated_quarantines'] == 1
    assert calls == []


@pytest.mark.parametrize('damage', ['unknown_write', 'writer_boundary', 'changed_request'])
def test_host_quarantine_revalidates_no_write_and_original_request(
    exchange, controlled_exchange, monkeypatch, damage,
):
    engine, action, calls = controlled_exchange
    request = host.publish(action, prompt='Review current frozen input.', timeout_seconds=60)
    now = time.time()
    monkeypatch.setattr(host.time, 'time', lambda: now + 61)
    reviewed = host.await_result(request)
    reference = host.failure_reference(action, reviewed)
    assert reference is not None
    if damage == 'unknown_write':
        state = json.loads(action.with_name('state.json').read_text())
        state['write_state'] = 'unknown'
        json_artifact.write_json_atomic(action.with_name('state.json'), state)
    elif damage == 'writer_boundary':
        json_artifact.write_json_atomic(action.with_name('writer-boundary.json'), {'write': 'unknown'})
    else:
        payload = json.loads(request.read_text())
        payload['expires_at'] += 60
        json_artifact.write_json_atomic(request, payload)
    before = action.read_bytes(), action.with_name('state.json').read_bytes()

    with pytest.raises(ValueError, match='review_quarantine_invalid'):
        engine.quarantine_review_failure(action, 'host_review_request_expired', host_failure_reference=reference)

    assert (action.read_bytes(), action.with_name('state.json').read_bytes()) == before
    assert calls == []


@pytest.mark.parametrize('failed_file', ['review-failure.json', item_failure.RECEIPT_FILE])
def test_host_isolation_persistence_failure_keeps_controller_blocked(
    exchange, controlled_exchange, monkeypatch, failed_file,
):
    engine, action, calls = controlled_exchange
    request = host.publish(action, prompt='Review current frozen input.', timeout_seconds=60)
    now = time.time()
    monkeypatch.setattr(host.time, 'time', lambda: now + 61)
    failed = host.await_result(request)
    original_write = json_artifact.write_json_atomic

    def unavailable(path, value, **kwargs):
        if Path(path).name == failed_file:
            raise OSError('offline filesystem unavailable')
        return original_write(path, value, **kwargs)

    monkeypatch.setattr(json_artifact, 'write_json_atomic', unavailable)
    monkeypatch.setattr(controller, '_preparation_executor', lambda: ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(host.approval_lifecycle, 'submit_readonly',
                        lambda executor, function, *args, **kwargs: executor.submit(function, *args))
    result = controller._run_dispatched_tasks(
        1, 1, project_root=action.parents[6], renew_actions=lambda paths: paths,
        prepare_action=lambda _: {'action': 'review_required', 'action_file': str(action)},
        review_worker=lambda _: failed, quarantine_review=engine.quarantine_review_failure,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {'status': 'healthy'}, strict_global_fifo=True,
    )

    value = result.outcomes[0].value
    assert value['status'] == 'failed'
    assert value['error_code'] == 'codex_review_quarantine_failed'
    assert value['failure_detail_code'] == 'host_review_request_expired'
    assert runtime_control.stop_requested() is True
    state = json.loads(action.with_name('state.json').read_text())
    assert item_failure.isolated(action.parent, state) is False
    report = reconciliation.reconcile(action.parents[4], pending_rows=[{
        'request_id': 'offline-host-failure-general_expense', 'received_at': '2026-10-10T09:00:00+08:00',
    }], handled_rows=[])
    assert report['quarantine']['pending_quarantined'] == 0
    if failed_file == item_failure.RECEIPT_FILE:
        assert report['continuation']['status'] == 'blocked'
    assert calls == []
