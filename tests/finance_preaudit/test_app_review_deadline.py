import hashlib
import json
from pathlib import Path
import time
import threading

import pytest

from apps.oa_finance_preaudit import attempt_state, host_chat_review as host
from tests.finance_preaudit.test_host_chat_review import exchange, draft_source, native_call
from tests.finance_preaudit.test_app_direct_dispatch import reject_live_preparation_credentials_and_alerts


def accept_native(exchange, action=None, *, timeout=60, slot=0, child=None, receive=True, request=None):
    original, draft, owner, original_child, *_ = exchange
    action = action or original
    child = child or original_child
    request = request or host.publish(action, prompt='Review this frozen action.', timeout_seconds=timeout)
    parent = original_child.with_name('parent.jsonl')
    dispatch = native_call(parent, 'followup_task', {'target': f'/root/reviewer_{slot}', 'message': str(request)},
                           'deadline-dispatch-' + str(slot), {})
    host.assign(request, slot=slot, owner=owner, source=parent, source_line=dispatch)
    if action != original:
        from tests.finance_preaudit.test_flow_engine_material_gaps import _uncertain_decision
        payload = _uncertain_decision(attempt_state.read_action(action), json.loads(action.with_name('review-packet.json').read_text()))
    else:
        payload = json.loads(draft.read_text())
    target = action.with_name('deadline-model.json')
    line = draft_source(child, request, target, owner, call_id='deadline-result-' + str(slot), payload=payload)
    if not receive:
        return request, target, line
    host.submit(request, target, owner=owner, source=child, source_line=line)
    return request


def test_first_read_after_deadline_keeps_the_timely_native_acceptance(exchange, monkeypatch):
    request = accept_native(exchange)
    original_request = request.read_bytes()
    expires = json.loads(original_request)['expires_at']
    monkeypatch.setattr(host.time, 'time', lambda: expires + 1)

    reviewed = host.poll_result(request)

    assert reviewed['status'] == 'reviewed'
    assert host.read_result(request) == reviewed
    assert host.poll_result(request) == reviewed
    assert request.read_bytes() == original_request
    assert not host.runtime_control.stop_requested()


@pytest.fixture
def native_writer(exchange, tmp_path):
    from apps.oa_finance_preaudit import flow_engine, production_write

    root = exchange[0].parents[4]
    workflows = ('general_expense', 'travel_expense', 'business_entertainment')
    rows = [{'request_id': 'offline-deadline-' + workflow, 'workflow_key': workflow,
             'received_at': f'2026-10-10T09:0{index}:00+08:00'} for index, workflow in enumerate(workflows)]
    policy = tmp_path / 'write-policy.json'
    host.guard.write(policy, {'schema_version': 1, 'kind': 'preaudit_write_policy', 'authorized_workflows': list(workflows)})
    writes = []
    active = []
    class OfflineOA:
        maximum_writers = 0

        def pending_rows(self, **_kwargs):
            return [row for row in rows if row['request_id'] not in writes]

        def submit_and_verify(self, request_id, remark):
            active.append(request_id)
            self.maximum_writers = max(self.maximum_writers, len(active))
            try:
                assert remark.startswith('AI审核结论：需人工判断')
                writes.append(request_id)
                return {'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True}
            finally:
                active.remove(request_id)

    api = OfflineOA()
    ports = production_write.build_ports(root=root, policy_file=policy, signing_secret='offline-deadline-secret', api_factory=lambda: api)
    engine = flow_engine.FlowEngine(
        root=root, discover_candidates=lambda workflow: [row for row in rows if row['workflow_key'] == workflow],
        discover_handled=lambda: [],
        collect_material=lambda request, attempt: {
            'schema_version': 1, 'kind': 'preaudit_material', 'request_id': request,
            'workflow_key': attempt.parents[2].name, 'workflow_name': 'offline form',
            'declared_fields': [{'field_name': 'period', 'value': '2026-08'}], 'resources': [],
            'prepared': {'images': [], 'structured_contents': [], 'manifest': []}, 'authority_facts': [], 'warnings': [],
        }, **ports,
    )
    actions = [Path(engine.next_action(workflow, mode='controlled_write')['action_file']) for workflow in workflows]
    manifest = exchange[5]
    host.approval_lifecycle.heartbeat(manifest['run_id'], manifest['controller_instance_id'], set(actions), [], writer_active=False)

    def complete(action, decision):
        target = action.with_name('writer-input.json')
        host.guard.write(target, decision)
        result = engine.complete_review(action, target)
        return None if result['action'] == 'closed' else result['reason']

    return engine, actions, api, writes, complete, policy


@pytest.mark.parametrize('after_deadline', [False, True])
def test_completion_revalidates_timely_result_after_fifo_wait(exchange, native_writer, monkeypatch, after_deadline):
    _, actions, api, writes, complete, _ = native_writer
    action = actions[0]
    request = accept_native(exchange, action)
    reviewed = host.read_result(request)
    if after_deadline:
        expires = json.loads(request.read_text())['expires_at']
        monkeypatch.setattr(host.time, 'time', lambda: expires + 1)

    result = host.complete(action, reviewed, completer=complete)

    assert result['status'] == 'completed', result
    assert writes == ['offline-deadline-general_expense'] and api.maximum_writers == 1
    receipt = json.loads(Path(attempt_state.read_action(action)['receipt_file']).read_text())
    assert all(receipt[key] is True for key in ('write', 'submit_ok', 'done_hit', 'postcheck_done_hit'))
    host.complete(action, reviewed, completer=complete)
    assert writes == ['offline-deadline-general_expense']


def test_completion_preserves_the_first_bounded_validation_failure(exchange, native_writer, monkeypatch):
    _, actions, _, writes, complete, _ = native_writer
    action = actions[0]
    request = accept_native(exchange, action)
    reviewed = host.read_result(request)
    material = json.loads(action.with_name('material.json').read_text())
    material['warnings'] = ['changed frozen material']
    host.guard.write(action.with_name('material.json'), material)
    monkeypatch.setattr(host.time, 'time', lambda: json.loads(request.read_text())['expires_at'] + 1)

    failed = host.complete(action, reviewed, completer=complete)

    assert failed['status'] == 'failed' and failed['error_code'] == 'host_review_result_untrusted'
    assert failed['failure_detail_code'] == 'host_review_request_untrusted'
    assert failed['exception_type'] == 'ValueError' and failed['operation'] == 'complete'
    assert failed['failure_recorded'] is True and writes == []
    assert failed['request_file'] == str(request)
    assert failed['request_sha256'] == reviewed['host_receipt']['request_sha256']
    assert failed['failure_phase'] == 'live_validation'
    registry = json.loads(host.registry_file(exchange[2]).read_text())
    first = registry['first_failure']
    assert first['operation'] == 'complete' and first['request_file'] == str(request)
    assert first['request_sha256'] == reviewed['host_receipt']['request_sha256']
    host.complete(action, reviewed, completer=complete)
    assert json.loads(host.registry_file(exchange[2]).read_text())['first_failure'] == first


@pytest.mark.parametrize('offset', [-.001, 0, 1])
def test_first_host_acceptance_must_precede_the_original_deadline(exchange, monkeypatch, offset):
    request, target, line = accept_native(exchange, receive=False)
    expires = json.loads(request.read_text())['expires_at']
    monkeypatch.setattr(host.time, 'time', lambda: expires + offset)
    if offset < 0:
        host.submit(request, target, owner=exchange[2], source=exchange[3], source_line=line)
        assert host.read_result(request)['status'] == 'reviewed'
    else:
        with pytest.raises(ValueError, match='host_review_request_expired'):
            host.submit(request, target, owner=exchange[2], source=exchange[3], source_line=line)
        assert not host._submission_file(request).exists()


def test_self_reported_acceptance_before_native_publication_is_untrusted(exchange, monkeypatch):
    from datetime import datetime

    request = accept_native(exchange)
    submitted_file = host._submission_file(request)
    submitted = json.loads(submitted_file.read_text())
    request_value = json.loads(request.read_text())
    registry_file = host.registry_file(exchange[2])
    registered = json.loads(registry_file.read_text())
    assigned = registered['assignments'][request_value['request_id']]
    native = json.loads(exchange[3].read_text().splitlines()[submitted['source']['line'] - 1])
    published_at = datetime.fromisoformat(native['timestamp']).timestamp()
    forged_time = (assigned['dispatched_at'] + published_at) / 2
    submitted['submitted_at'] = assigned['submitted_at'] = forged_time
    host.guard.write(submitted_file, submitted)
    assigned['submission_sha256'] = hashlib.sha256(submitted_file.read_bytes()).hexdigest()
    host.guard.write(registry_file, registered)
    monkeypatch.setattr(host.time, 'time', lambda: request_value['expires_at'] + 1)

    failed = host.poll_result(request)

    assert failed['status'] == 'failed' and failed['failure_detail_code'] == 'host_review_submission_untrusted'


def test_rolling_fifo_writes_timely_native_results_after_the_head_expires(exchange, native_writer, monkeypatch, request):
    from scripts import finance_preaudit_concurrency_probe as controller
    from apps.oa_finance_preaudit import production_config

    engine, actions, api, writes, complete, _ = native_writer
    owner, manifest = exchange[2], exchange[5]
    monkeypatch.setattr(host.approval_lifecycle, 'submit_readonly',
                        lambda executor, function, *args, **kwargs: executor.submit(function, *args))
    monkeypatch.setattr(production_config, 'review_timeout_seconds', lambda: 60)
    observed = set()
    original_poll = host.poll_result
    def observe(path):
        result = original_poll(path)
        if result is not None and result['status'] == 'reviewed':
            observed.add(Path(json.loads(path.read_text())['action_file']))
        return result
    monkeypatch.setattr(host, 'poll_result', observe)
    prepared = iter(actions)
    def prepare(_selection):
        action = next(prepared, None)
        return {'action': 'review_required', 'action_file': str(action)} if action else {'action': 'no_action'}
    results, errors = [], []
    def run():
        try:
            results.append(controller._run_dispatched_tasks(
                3, 3, project_root=actions[0].parents[6], renew_actions=lambda paths: paths,
                prepare_action=prepare,
                complete_review=lambda action, result: host.complete(action, result, completer=complete),
                quarantine_review=engine.quarantine_review_failure,
                executor_factory=lambda *_: pytest.fail('App review executor created'),
                strict_global_fifo=True, runtime_status=lambda: {'status': 'healthy'},
                heartbeat_interval_sec=.01, run_id=manifest['run_id'],
                controller_instance_id=manifest['controller_instance_id'],
            ))
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        published = []
        while len(published) < 3 and thread.is_alive() and time.monotonic() < deadline:
            published = host.pending(owner=owner)
            time.sleep(.01)
        assert not errors and len(published) == 3
        paths = {Path(json.loads(Path(item['request_file']).read_text())['action_file']): Path(item['request_file']) for item in published}
        for index, action in enumerate(actions):
            accept_native(exchange, action, slot=index, child=Path(request.node.native_reviewers[index]['session_file']),
                          receive=index != 0, request=paths[action])
        while len(observed) < 2 and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert observed == set(actions[1:]) and writes == []
        expires = max(json.loads(path.read_text())['expires_at'] for path in paths.values())
        monkeypatch.setattr(host.time, 'time', lambda: expires + 1)
        thread.join(5)
        assert not errors and not thread.is_alive(), errors
        assert [item.value['status'] for item in results[0].outcomes] == ['quarantined', 'completed', 'completed']
        assert writes == ['offline-deadline-travel_expense', 'offline-deadline-business_entertainment']
        assert api.maximum_writers == 1 and results[0].processes_reaped
        assert not host.runtime_control.stop_requested()
        for action in actions[1:]:
            receipt = json.loads(Path(attempt_state.read_action(action)['receipt_file']).read_text())
            assert all(receipt[key] is True for key in ('write', 'submit_ok', 'done_hit', 'postcheck_done_hit'))
    finally:
        if thread.is_alive():
            host.runtime_control.request_cycle_stop()
            thread.join(5)


@pytest.mark.parametrize('damage,code', [
    ('request', 'host_review_result_untrusted'),
    ('receipt', 'host_review_result_untrusted'),
    ('decision', 'host_review_source_untrusted'),
    ('source', 'host_review_source_untrusted'),
    ('assignment', 'host_review_submission_untrusted'),
    ('parent', 'host_review_parent_lost_or_closed'),
    ('closed', 'stop_requested'),
    ('stopped', 'stop_requested'),
])
def test_accepted_fifo_result_keeps_current_prewrite_guards(exchange, native_writer, monkeypatch, damage, code):
    _, actions, _, writes, complete, _ = native_writer
    action = actions[0]
    request = accept_native(exchange, action)
    reviewed = host.read_result(request)
    expires = json.loads(request.read_text())['expires_at']
    if damage == 'request':
        value = json.loads(request.read_text())
        value['prompt'] += 'changed request'
        host.guard.write(request, value)
    elif damage == 'receipt':
        target = host._submission_file(request)
        value = json.loads(target.read_text())
        value['submitted_at'] -= .1
        host.guard.write(target, value)
    elif damage == 'decision':
        target = action.with_name('deadline-model.json')
        value = json.loads(target.read_text())
        value['overall'] = 'pass'
        host.guard.write(target, value)
    elif damage == 'source':
        exchange[3].write_text(exchange[3].read_text().replace('deadline-result-0', 'changed-result'))
    elif damage == 'assignment':
        target = host.registry_file(exchange[2])
        value = json.loads(target.read_text())
        value['assignments'][json.loads(request.read_text())['request_id']]['reviewer_task_id'] = value['reviewers'][1]['reviewer_task_id']
        host.guard.write(target, value)
    elif damage == 'closed':
        host.close(owner=exchange[2], reason='cancelled')
    elif damage == 'stopped':
        host.runtime_control.request_cycle_stop()
    now = expires + (301 if damage == 'parent' else 1)
    monkeypatch.setattr(host.time, 'time', lambda: now)

    failed = host.complete(action, reviewed, completer=complete)

    assert failed['status'] == 'failed' and failed['failure_detail_code'] == code
    assert writes == [] and host.runtime_control.stop_requested()


@pytest.mark.parametrize('damage', ['draft_only', 'receipt_missing', 'registry_acceptance_missing', 'forged_acceptance'])
def test_post_deadline_files_cannot_replace_the_host_acceptance(exchange, monkeypatch, damage):
    if damage == 'draft_only':
        request, _, _ = accept_native(exchange, receive=False)
    else:
        request = accept_native(exchange)
        if damage == 'receipt_missing':
            host._submission_file(request).unlink()
        else:
            target = host.registry_file(exchange[2])
            value = json.loads(target.read_text())
            assigned = value['assignments'][json.loads(request.read_text())['request_id']]
            if damage == 'registry_acceptance_missing':
                assigned['state'] = 'assigned'
                assigned.pop('submission_sha256')
            else:
                assigned['submitted_at'] -= 1
            host.guard.write(target, value)
    monkeypatch.setattr(host.time, 'time', lambda: json.loads(request.read_text())['expires_at'] + 1)

    failed = host.poll_result(request)

    assert failed['status'] == 'failed'
    assert failed['write_state'] == 'not_requested'


@pytest.mark.parametrize('unrecordable', [False, True])
def test_completion_unknown_exception_keeps_a_bounded_failure_without_private_text(exchange, native_writer, monkeypatch, unrecordable):
    _, actions, _, writes, _, _ = native_writer
    request = accept_native(exchange, actions[0])
    reviewed = host.read_result(request)
    def broken(*_args):
        raise RuntimeError('synthetic-private-credential-and-response')
    if unrecordable:
        original_write = host.guard.write
        def write(path, value):
            if path == host.registry_file(exchange[2]):
                raise OSError('synthetic-private-filesystem')
            return original_write(path, value)
        monkeypatch.setattr(host.guard, 'write', write)
    monkeypatch.setattr(host.time, 'time', lambda: json.loads(request.read_text())['expires_at'] + 1)

    failed = host.complete(actions[0], reviewed, completer=broken)

    assert failed['status'] == 'failed' and failed['failure_detail_code'] == 'host_review_exception'
    assert failed['exception_type'] == 'RuntimeError' and failed['failure_phase'] == 'write'
    assert failed['failure_recorded'] is not unrecordable and writes == []
    assert 'synthetic-private' not in str(failed) + host.registry_file(exchange[2]).read_text()


@pytest.mark.parametrize('damage', ['authorization', 'unknown_write'])
def test_timely_acceptance_does_not_restore_current_write_eligibility(exchange, native_writer, monkeypatch, damage):
    _, actions, _, writes, complete, policy = native_writer
    action = actions[0]
    request = accept_native(exchange, action)
    reviewed = host.read_result(request)
    if damage == 'authorization':
        host.guard.write(policy, {'schema_version': 1, 'kind': 'preaudit_write_policy', 'authorized_workflows': ['travel_expense']})
    else:
        state = json.loads(action.with_name('state.json').read_text())
        state['write_state'] = 'unknown'
        host.guard.write(action.with_name('state.json'), state)
    monkeypatch.setattr(host.time, 'time', lambda: json.loads(request.read_text())['expires_at'] + 1)

    failed = host.complete(action, reviewed, completer=complete)

    assert failed['status'] == 'failed' and writes == []
    if damage == 'authorization':
        assert failed['error_code'] == 'write_authorization_invalid'
    else:
        assert json.loads(action.with_name('state.json').read_text())['write_state'] == 'unknown'
