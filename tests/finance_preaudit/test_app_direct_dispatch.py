from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import host_chat_review as host, production_config
from scripts import finance_preaudit_concurrency_probe as controller
from tests.finance_preaudit.test_host_chat_review import exchange, draft_source, native_call
from tests.finance_preaudit.test_host_review_failure import controlled_exchange
from tests.finance_preaudit.test_flow_engine_material_gaps import _uncertain_decision


@pytest.fixture(autouse=True)
def reject_live_preparation_credentials_and_alerts(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail('Live preparation, credentials and notifications are forbidden in direct dispatch tests')

    monkeypatch.setattr(controller, '_preparation_executor', lambda: ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(controller, '_prepare_one', forbidden)
    monkeypatch.setattr(controller.codex_review_worker, '_execute_codex', forbidden)
    monkeypatch.setattr(controller.codex_review_worker, '_codex_command', forbidden)
    monkeypatch.setattr(controller.cli.legacy_flow, 'load_config', forbidden)
    monkeypatch.setattr(controller.cli.legacy_flow, 'load_all_configs', forbidden)
    monkeypatch.setattr(controller.session_alert, 'alert_once', forbidden)
    monkeypatch.setattr(controller.session_alert, 'send_message', forbidden)


def test_poll_result_returns_pending_then_the_original_native_submission(exchange):
    action, draft, owner, child, *_ = exchange
    request = host.publish(action, prompt='Read this frozen action only.', timeout_seconds=60)

    assert host.poll_result(request) is None
    assert not runtime_control.stop_requested()
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request)}, 'poll-dispatch', {})
    host.assign(request, slot=0, owner=owner, source=parent, source_line=line)
    line = draft_source(child, request, draft, owner)
    host.submit(request, draft, owner=owner, source=child, source_line=line)

    reviewed = host.poll_result(request)

    assert reviewed['status'] == 'reviewed'
    assert reviewed == host.read_result(request)


def test_pending_poll_rejects_a_changed_original_deadline(exchange):
    action, _, owner, *_ = exchange
    request = host.publish(action, prompt='Keep the original frozen deadline.', timeout_seconds=60)
    changed = json.loads(request.read_text())
    changed['expires_at'] += 900
    host.guard.write(request, changed)

    failed = host.poll_result(request)

    assert failed is not None and failed['failure_detail_code'] == 'host_review_request_untrusted'
    assert runtime_control.stop_requested()
    assert not json.loads(host.registry_file(owner).read_text()).get('request_failures')


def test_parent_cli_reports_the_current_native_slots_and_rejects_another_round(exchange, capsys):
    from uuid import uuid4

    _, _, owner, _, _, manifest, _ = exchange
    arguments = ['--operation', 'status', '--owner-task-id', owner, '--run-id', manifest['run_id']]

    assert host.main(arguments) == 0

    status = json.loads(capsys.readouterr().out)
    assert status['ready'] and status['capacity'] == 3
    assert status['run_id'] == manifest['run_id'] and len(status['slots']) == 3
    with pytest.raises(ValueError, match='run_untrusted'):
        host.main([*arguments[:-1], uuid4().hex])



@pytest.mark.parametrize('exchange', [False], indirect=True)
def test_first_native_heartbeat_uses_the_original_bootstrap_clock(exchange, monkeypatch, request):
    _, _, owner, child, *_ = exchange
    parent = child.with_name('parent.jsonl')
    host.register(request.node.native_reviewers, owner=owner, source=parent, **request.node.original_binding)
    now = time.time()
    monkeypatch.setattr(host.time, 'time', lambda: now + 301)

    assert host.round_status(owner=owner)['ready'] is False

    monkeypatch.setattr(host.time, 'time', lambda: now + 901)
    with pytest.raises(ValueError, match='parent_lost_or_closed'):
        host.round_status(owner=owner)


@pytest.mark.parametrize('watchdog', [False, True])
def test_native_dispatch_publishes_without_a_python_review_executor(exchange, controlled_exchange, monkeypatch, watchdog):
    engine, action, oa_calls = controlled_exchange
    _, _, owner, child, _, manifest, _ = exchange
    monkeypatch.setattr(host.approval_lifecycle, 'submit_readonly',
                        lambda executor, function, *args, **kwargs: executor.submit(function, *args))
    results, errors, writes = [], [], []

    def complete(action_file, reviewed):
        writes.append(action_file)
        assert reviewed == host.read_result(Path(reviewed['host_receipt']['request_file']))
        return {'status': 'completed', 'write_state': 'submitted', 'business_state': 'needs_human'}

    def run():
        try:
            results.append(controller._run_dispatched_tasks(
                1, 3, project_root=action.parents[6], renew_actions=lambda paths: paths,
                prepare_action=lambda _: {'action': 'review_required', 'action_file': str(action)},
                complete_review=complete, quarantine_review=engine.quarantine_review_failure,
                executor_factory=lambda *_: pytest.fail('App review executor created'),
                strict_global_fifo=True, runtime_status=lambda: {'status': 'healthy'},
                heartbeat_interval_sec=.01, task_timeout_sec=.1 if watchdog else None, run_id=manifest['run_id'],
                controller_instance_id=manifest['controller_instance_id'],
            ))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    parent = child.with_name('parent.jsonl')
    deadline = time.monotonic() + 5
    try:
        published = []
        while not published and thread.is_alive() and time.monotonic() < deadline:
            published = host.pending(owner=owner)
            time.sleep(.01)
        assert not errors, errors
        assert len(published) == 1
        request = Path(published[0]['request_file'])
        if watchdog:
            thread.join(2)
            assert not errors and not thread.is_alive()
            assert results[0].outcomes[0].status == 'timed_out'
            assert results[0].outcomes[0].error_code == 'worker_task_timeout'
            assert writes == [] and oa_calls == [] and runtime_control.stop_requested()
            return
        line = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request)}, 'direct-dispatch', {})
        host.assign(request, slot=0, owner=owner, source=parent, source_line=line)
        packet = json.loads(action.with_name('review-packet.json').read_text())
        draft = action.with_name('direct-model-decision.json')
        line = draft_source(child, request, draft, owner,
                            payload=_uncertain_decision(json.loads(action.read_text()), packet))
        host.submit(request, draft, owner=owner, source=child, source_line=line)
        thread.join(5)
        assert not errors and not thread.is_alive()
        assert writes == [action] and oa_calls == []
        assert results[0].completed == 1 and results[0].processes_reaped
    finally:
        if thread.is_alive():
            runtime_control.request_cycle_stop()
            thread.join(5)


@pytest.mark.parametrize('exchange', [False], indirect=True)
@pytest.mark.parametrize('parent_lost', [False, True])
def test_native_dispatch_keeps_bootstrap_heartbeat_without_blocking_publication(
    exchange, controlled_exchange, monkeypatch, request, parent_lost,
):
    engine, action, _ = controlled_exchange
    _, _, owner, child, _, manifest, _ = exchange
    parent = child.with_name('parent.jsonl')
    monkeypatch.setattr(controller, '_preparation_executor', lambda: ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(host.approval_lifecycle, 'submit_readonly',
                        lambda executor, function, *args, **kwargs: executor.submit(function, *args))
    beats = []
    original = host.approval_lifecycle.heartbeat
    def heartbeat(*args, **kwargs):
        beats.append(list(args[3]))
        return original(*args, **kwargs)
    monkeypatch.setattr(host.approval_lifecycle, 'heartbeat', heartbeat)
    results, errors, prepared = [], [], []
    def prepare(_selection):
        prepared.append(action)
        return {'action': 'review_required', 'action_file': str(action)}
    def run():
        try:
            results.append(controller._run_dispatched_tasks(
                1, 3, project_root=action.parents[6], renew_actions=lambda paths: paths,
                prepare_action=prepare,
                complete_review=lambda *_: {'status': 'completed', 'business_state': 'needs_human', 'write_state': 'submitted'},
                executor_factory=lambda *_: pytest.fail('Bootstrap created review executor'),
                strict_global_fifo=True, runtime_status=lambda: {'status': 'healthy'},
                heartbeat_interval_sec=.01, run_id=manifest['run_id'],
                controller_instance_id=manifest['controller_instance_id'],
            ))
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while len(beats) < 2 and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(beats) >= 2 and not errors and thread.is_alive()
        assert host.pending(owner=owner) == [] and host.round_status(owner=owner)['ready'] is False
        assert prepared == []
        if parent_lost:
            now = time.time()
            monkeypatch.setattr(host.time, 'time', lambda: now + 901)
            thread.join(5)
            assert not errors and not thread.is_alive()
            assert results[0].stopped == 1 and results[0].started == 0
            assert runtime_control.stop_requested() and host.pending(owner=owner) == []
            return
        host.register(request.node.native_reviewers, owner=owner, source=parent, **request.node.original_binding)
        line = native_call(parent, 'functions.exec', 'heartbeat ' + owner, 'direct-boot-heartbeat', {})
        host.heartbeat(owner=owner, source=parent, source_line=line)
        from tests.finance_preaudit.test_host_chat_review import wait_request
        published = wait_request(action, owner)
        frozen = json.loads(published.read_text())
        assert frozen['expires_at'] - frozen['created_at'] == 1800
        assert any(1850 < deadline - time.time() <= 1860 for deadlines in beats for deadline in deadlines)
        line = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(published)}, 'direct-boot-dispatch', {})
        host.assign(published, slot=0, owner=owner, source=parent, source_line=line)
        packet = json.loads(action.with_name('review-packet.json').read_text())
        payload = _uncertain_decision(json.loads(action.read_text()), packet)
        draft = action.with_name('direct-bootstrap-model.json')
        line = draft_source(child, published, draft, owner, call_id='direct-boot-result', payload=payload)
        host.submit(published, draft, owner=owner, source=child, source_line=line)
        thread.join(5)
        assert not errors and not thread.is_alive() and results[0].completed == 1
    finally:
        if thread.is_alive():
            runtime_control.request_cycle_stop()
            thread.join(5)


@pytest.mark.parametrize('failure_kind,writer_unknown', [
    ('expired', False), ('expired', True),
    ('all_fenced', False), ('shared_source', False),
])
def test_native_dispatch_polls_original_three_requests_without_review_executor(
    exchange, controlled_exchange, monkeypatch, request, failure_kind, writer_unknown,
):
    engine, first, oa_calls = controlled_exchange
    _, _, owner, first_child, _, manifest, _ = exchange
    actions = [first, *(Path(engine.next_action(workflow)['action_file'])
                        for workflow in ('travel_expense', 'business_entertainment'))]
    monkeypatch.setattr(host.approval_lifecycle, 'submit_readonly',
                        lambda executor, function, *args, **kwargs: executor.submit(function, *args))
    monkeypatch.setattr(controller, '_preparation_executor', lambda: ThreadPoolExecutor(max_workers=1))
    if failure_kind in {'expired', 'all_fenced'}:
        monkeypatch.setattr(production_config, 'review_timeout_seconds', lambda: 60)
    prepared, writes, results, errors = iter(actions), [], [], []
    native_published = threading.Event()
    original_poll = host.poll_result
    monkeypatch.setattr(host, 'poll_result', lambda path: original_poll(path) if native_published.is_set() else None)
    def prepare(_selection):
        action = next(prepared, None)
        return {'action': 'review_required', 'action_file': str(action)} if action else {'action': 'no_action'}
    def complete(action, result):
        assert native_published.wait(3), 'Native sibling submissions did not finish'
        if action == first:
            return host.complete(action, result)
        writes.append(action)
        if writer_unknown:
            return {'status': 'failed', 'write_state': 'unknown', 'error_code': 'writer_result_unknown'}
        return {'status': 'completed', 'business_state': 'needs_human', 'write_state': 'submitted', 'error_code': None}
    def run():
        try:
            results.append(controller._run_dispatched_tasks(
                6 if failure_kind == 'all_fenced' else 3, 3, project_root=first.parents[6], renew_actions=lambda paths: paths,
                prepare_action=prepare, complete_review=complete,
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
    handled = set()
    reviewers = host.registered_reviewers(owner=owner)
    parent = first_child.with_name('parent.jsonl')
    deadline = time.monotonic() + 8
    try:
        while thread.is_alive() and time.monotonic() < deadline:
            for published in host.pending(owner=owner):
                path = Path(published['request_file'])
                if path in handled:
                    continue
                action = Path(json.loads(path.read_text())['action_file'])
                index = actions.index(action)
                line = native_call(parent, 'followup_task', {
                    'target': reviewers[index]['agent_path'], 'message': str(path),
                }, f'direct-dispatch-{index}', {})
                host.assign(path, slot=index, owner=owner, source=parent, source_line=line)
                child = Path(request.node.native_reviewers[index]['session_file'])
                packet = json.loads(action.with_name('review-packet.json').read_text())
                payload = _uncertain_decision(json.loads(action.read_text()), packet)
                if failure_kind == 'all_fenced' or (failure_kind == 'expired' and index == 0):
                    handled.add(path)
                    continue
                draft = action.with_name(f'direct-native-{index}.json')
                line = draft_source(child, path, draft, owner, call_id=f'direct-review-{index}', payload=payload)
                host.submit(path, draft, owner=owner, source=child, source_line=line)
                handled.add(path)
            if len(handled) == 3:
                if failure_kind == 'shared_source':
                    Path(request.node.native_reviewers[1]['session_file']).write_text('{}\n')
                    failure_kind = 'shared_source_observed'
                native_published.set()
            if failure_kind in {'expired', 'all_fenced'} and len(handled) == 3:
                expirations = [json.loads(path.read_text())['expires_at'] for path in handled]
                now = (max(expirations) if failure_kind == 'all_fenced' else min(expirations)) + .0001
                monkeypatch.setattr(host.time, 'time', lambda: now)
                failure_kind = 'all_fenced_observed' if failure_kind == 'all_fenced' else 'expiry_observed'
            time.sleep(.01)
        assert not errors, errors
        assert len(handled) == 3 and not thread.is_alive()
        if failure_kind == 'shared_source_observed':
            assert writes == [] and oa_calls == []
            assert runtime_control.stop_requested()
            assert all(
                (item.status == 'stopped' and item.value is None)
                or (item.status in {'completed', 'stopped'} and isinstance(item.value, dict)
                    and item.value.get('status') in {'failed', 'stopped'})
                for item in results[0].outcomes
            )
            assert results[0].processes_reaped is True
            assert not json.loads(host.registry_file(owner).read_text()).get('request_failures')
            return
        if failure_kind == 'all_fenced_observed':
            assert writes == [] and oa_calls == []
            assert [item.value['status'] for item in results[0].outcomes] == ['quarantined'] * 3
            assert results[0].admission_closed is True
            assert results[0].admission_reason == 'host_review_slots_fenced'
            assert results[0].requested == 3
            status = host.round_status(owner=owner)
            assert status['capacity'] == 0 and all(slot['fenced'] for slot in status['slots'])
            assert {Path(item['request_file']) for slot in status['slots'] for item in slot['failed_requests']} == handled
            assert not runtime_control.stop_requested()
            return
        assert writes == (actions[1:2] if writer_unknown else actions[1:]) and oa_calls == []
        assert results[0].outcomes[0].value['status'] == 'quarantined'
        if not writer_unknown:
            assert [item.value['status'] for item in results[0].outcomes] == ['quarantined', 'completed', 'completed']
        if not writer_unknown:
            assert host.registered_reviewers(owner=owner) == reviewers
        assert runtime_control.stop_requested() is writer_unknown
    finally:
        native_published.set()
        if thread.is_alive():
            runtime_control.request_cycle_stop()
            thread.join(5)
