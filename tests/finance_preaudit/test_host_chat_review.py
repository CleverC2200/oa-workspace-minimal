from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

import pytest

from apps import json_artifact
from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import host_chat_review as host, codex_review_worker as worker, flow_engine, controller_handoff
from scripts import finance_preaudit_concurrency_probe as controller, finance_preaudit_run


@pytest.fixture
def exchange(tmp_path, monkeypatch, request):
    root = tmp_path / 'project'
    root.mkdir()
    engine = flow_engine.FlowEngine(root=root / 'runtime/finance_preaudit_flows',
        discover_candidates=lambda workflow: [{'request_id': 'offline-request',
            'received_at': '2026-10-10T09:00:00+08:00', 'workflow_key': workflow}],
        collect_material=lambda request, _: {'schema_version': 1, 'kind': 'preaudit_material',
            'request_id': request, 'workflow_key': 'business_support_fee', 'workflow_name': '业务支持费',
            'declared_fields': [], 'resources': [],
            'prepared': {'images': [], 'structured_contents': [], 'manifest': []},
            'authority_facts': [], 'warnings': []})
    action = Path(engine.next_action('business_support_fee')['action_file'])
    packet = json.loads(action.with_name('review-packet.json').read_text())
    decision = {'overall': 'needs_human', 'items': [
        {'item_key': item['item_key'], 'status': 'uncertain', 'conclusion': '自动读取材料不足，需财务核对原单。',
         'evidence_refs': [], 'missing_evidence': ['缺少审核材料']} for item in packet['items']]}
    draft = tmp_path / 'draft.json'
    json_artifact.write_json_atomic(draft, decision)
    draft.chmod(0o600)
    from tests.finance_preaudit.test_app_reviewer_slots import native_sessions, original_execution
    owner, parent, reviewers = native_sessions(tmp_path, monkeypatch)
    source = Path(reviewers[0]['session_file'])
    monkeypatch.setenv(host.TRANSPORT_ENV, 'host_chat')
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    monkeypatch.setenv(host.guard.OWNER_ENV, owner)
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'control/controller.lock'))
    monkeypatch.setenv(host.guard.EXECUTION_OWNERS_ENV, json.dumps([host.guard.process_identity(os.getpid())]))
    monkeypatch.setattr(runtime_control, 'PROJECT_ROOT', root)
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({'review_configuration': host.IDENTITY})
        with host.approval_lifecycle.run_scope(manifest, root):
            binding = original_execution(owner, manifest)
            monkeypatch.delenv(host.guard.EXECUTION_OWNERS_ENV, raising=False)
            request.node.original_binding = binding
            request.node.native_reviewers = reviewers
            if getattr(request, 'param', True):
                host.register(reviewers, owner=owner, source=parent, **binding)
                line = native_call(parent, 'functions.exec', 'heartbeat ' + owner, 'initial-heartbeat', {})
                host.heartbeat(owner=owner, source=parent, source_line=line)
            host.approval_lifecycle.heartbeat(manifest['run_id'], manifest['controller_instance_id'], {action}, [], writer_active=False)
            task = {'project_root': str(root), 'action_file': str(action), 'timeout_seconds': 60}
            yield action, draft, owner, source, task, manifest, engine
        if controller_handoff._read_manifest()['state'] == 'running':
            controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')


def native_call(source, name, arguments, call_id, output):
    from datetime import datetime, timezone
    line = sum(1 for _ in source.open()) + 1
    with source.open('a') as stream:
        stream.write(json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(), 'type': 'response_item',
            'payload': {'type': 'function_call' if name != 'functions.exec' else 'custom_tool_call',
                'name': name, 'call_id': call_id,
                'arguments' if name != 'functions.exec' else 'input': json.dumps(arguments) if name != 'functions.exec' else arguments}}) + '\n')
        stream.write(json.dumps({'type': 'response_item', 'payload': {'type': 'function_call_output',
            'call_id': call_id, 'output': output if isinstance(output, str) else json.dumps(output)}}) + '\n')
    return line


def wait_request(action, owner, *, kind='finance', timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in host.pending(owner=owner):
            path = Path(item['request_file'])
            if item['review_kind'] == kind and json.loads(path.read_text())['action_file'] == str(action):
                return path
        time.sleep(.01)
    raise AssertionError('Current round did not publish the requested frozen action')


def test_cancelled_round_reuses_frozen_attempt_without_overwriting_old_request(exchange, tmp_path, monkeypatch):
    from tests.finance_preaudit.test_app_reviewer_slots import native_sessions, original_execution
    action, draft, owner, child, task, manifest, _ = exchange
    old_request = host.publish(action, prompt='Old cancelled review.', timeout_seconds=60)
    old_bytes = old_request.read_bytes()
    old_source = draft_source(child, old_request, draft, owner)
    host.close(owner=owner, reason='cancelled')
    controller_handoff.finish(manifest, status='failed', children_reaped=True, health='degraded')
    assert runtime_control.clear_cycle_stop()
    new_owner, parent, reviewers = native_sessions(tmp_path / 'next-round', monkeypatch)
    monkeypatch.setenv(host.guard.OWNER_ENV, new_owner)
    monkeypatch.setattr(controller_handoff, 'CONTROLLER_INSTANCE_ID', uuid4().hex)
    current = controller_handoff.begin({'review_configuration': host.IDENTITY})
    with host.approval_lifecycle.run_scope(current, runtime_control.PROJECT_ROOT):
        binding = original_execution(new_owner, current)
        host.register(reviewers, owner=new_owner, source=parent, **binding)
        line = native_call(parent, 'functions.exec', 'heartbeat ' + new_owner, 'first-new-heartbeat', {})
        host.heartbeat(owner=new_owner, source=parent, source_line=line)
        host.approval_lifecycle.heartbeat(current['run_id'], current['controller_instance_id'], {action}, [], writer_active=False)
        results = []
        thread = threading.Thread(target=lambda: results.append(controller._review_only(task)))
        thread.start()
        try:
            request = wait_request(action, new_owner)
            assert request != old_request and old_request.read_bytes() == old_bytes
            child = Path(reviewers[0]['session_file'])
            dispatch = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request)}, 'new-dispatch', {})
            host.assign(request, slot=0, owner=new_owner, source=parent, source_line=dispatch)
            target = draft.with_name('new-round-decision.json')
            source = draft_source(child, request, target, new_owner, payload=json.loads(draft.read_text()))
            host.submit(request, target, owner=new_owner, source=child, source_line=source)
            thread.join(3)
            assert results[0]['status'] == 'reviewed'
            assert host.pending(owner=new_owner) == []
            with pytest.raises(ValueError):
                host.submit(old_request, draft, owner=new_owner, source=Path(exchange[3]), source_line=old_source)
            assert old_request.read_bytes() == old_bytes
        finally:
            host.close(owner=new_owner, reason='cancelled')
            thread.join(3)
            controller_handoff.finish(current, status='failed', children_reaped=True, health='degraded')



def draft_source(source, request_file, target, owner, *, call_id='actual-host-call', payload=None):
    payload = payload if payload is not None else json.loads(target.read_text())
    if target.exists():
        target.unlink()
    receipt = host.publish_draft(request_file, target, payload, owner=owner)
    code = 'text(await tools.exec_command(' + json.dumps({'cmd': host.draft_command(request_file, target, payload, owner=owner)}) + '));'
    return native_call(source, 'functions.exec', code, call_id, {'exit_code': 0, 'output': json.dumps(receipt) + '\n'})


@pytest.mark.parametrize('directory', ['same_project', 'other_project', 'relative_project', 'extra_argument'])
def test_native_publication_allows_only_the_current_project_workdir(exchange, directory):
    action, draft, owner, child, *_ = exchange
    thread, results, request = start(exchange)
    command = host.draft_command(request, draft, json.loads(draft.read_text()), owner=owner)
    arguments = {'cmd': command, 'workdir': str(runtime_control.PROJECT_ROOT)}
    if directory == 'other_project':
        arguments['workdir'] = str(runtime_control.PROJECT_ROOT.parent)
    elif directory == 'relative_project':
        arguments['workdir'] = '.'
    elif directory == 'extra_argument':
        arguments['shell'] = '/bin/sh'
    output = json.loads(child.read_text().splitlines()[2])['payload']['output']
    line = native_call(child, 'functions.exec', 'text(await tools.exec_command(' + json.dumps(arguments) + '));',
                       'publication-in-current-project', output)
    try:
        if directory == 'same_project':
            assert host.submit(request, draft, owner=owner, source=child, source_line=line)['status'] == 'published'
        else:
            with pytest.raises(ValueError, match='source_untrusted'):
                host.submit(request, draft, owner=owner, source=child, source_line=line)
    finally:
        host.close(owner=owner, reason='cancelled')
        thread.join(3)
    assert not thread.is_alive()


def test_public_draft_command_emits_one_json_receipt_without_project_warning(exchange, monkeypatch):
    import shlex
    import shutil
    import subprocess
    if shutil.which('uv') is None:
        pytest.skip('uv is required for the native publication command')
    _, draft, owner, _, *_ = exchange
    thread, _, request = start(exchange)
    target = request.with_name('native-cli-draft.json')
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_STOP_FILE', str(request.parent / 'isolated-stop.flag'))
    monkeypatch.setenv('UV_OFFLINE', '1')
    try:
        result = subprocess.run(shlex.split(host.draft_command(request, target, json.loads(draft.read_text()), owner=owner)),
            cwd=Path(host.__file__).resolve().parents[2], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ''
        receipt = json.loads(result.stdout)
        assert receipt['kind'] == 'preaudit_host_draft_publication'
        assert receipt['decision_file'] == str(target)
        assert json.loads(target.read_text()) == json.loads(draft.read_text())
    finally:
        host.close(owner=owner, reason='cancelled')
        thread.join(3)
    assert not thread.is_alive()


def test_public_draft_command_carries_round_controls_into_independent_exec(exchange):
    import shlex
    import subprocess
    _, draft, owner, child, *_ = exchange
    thread, results, request = start(exchange)
    target = request.with_name('independent-exec-draft.json')
    command = host.draft_command(request, target, json.loads(draft.read_text()), owner=owner)
    environment = dict(os.environ, UV_OFFLINE='1')
    environment.pop('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', None)
    environment.pop('OA_FINANCE_PREAUDIT_STOP_FILE', None)
    try:
        result = subprocess.run(shlex.split(command), env=environment,
            cwd=Path(host.__file__).resolve().parents[2], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        line = native_call(child, 'functions.exec',
            'text(await tools.exec_command(' + json.dumps({'cmd': command}) + '));',
            'independent-exec-publication', {'exit_code': result.returncode, 'output': result.stdout})
        assert host.submit(request, target, owner=owner, source=child, source_line=line)['status'] == 'published'
        thread.join(3)
        assert results[0]['status'] == 'reviewed'
        assert host.read_result(request)['write_state'] == 'not_requested'
    finally:
        host.close(owner=owner, reason='cancelled')
        thread.join(3)
    assert not thread.is_alive()


@pytest.mark.parametrize('route', ['historical_bare', 'foreign_controller', 'foreign_stop', 'extra_env'])
def test_native_publication_checks_fixed_controls_and_preserves_historical_bare_source(exchange, route):
    import shlex
    _, draft, owner, child, *_ = exchange
    thread, _, request = start(exchange)
    command = shlex.split(host.draft_command(request, draft, json.loads(draft.read_text()), owner=owner))
    if route == 'historical_bare':
        command = command[3:]
    elif route == 'foreign_controller':
        command[1] = 'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE=' + str(request.parent / 'foreign.lock')
    elif route == 'foreign_stop':
        command[2] = 'OA_FINANCE_PREAUDIT_STOP_FILE=' + str(request.parent / 'foreign-stop.flag')
    else:
        command.insert(3, 'UNAPPROVED_ENV=1')
    output = json.loads(child.read_text().splitlines()[2])['payload']['output']
    line = native_call(child, 'functions.exec',
        'text(await tools.exec_command(' + json.dumps({'cmd': shlex.join(command)}) + '));',
        'fixed-control-publication', output)
    try:
        if route == 'historical_bare':
            assert host.submit(request, draft, owner=owner, source=child, source_line=line)['status'] == 'published'
        else:
            with pytest.raises(ValueError, match='source_untrusted'):
                host.submit(request, draft, owner=owner, source=child, source_line=line)
    finally:
        host.close(owner=owner, reason='cancelled')
        thread.join(3)
    assert not thread.is_alive()


def test_independent_publication_enforces_the_original_stop_file(exchange):
    import shlex
    import subprocess
    _, draft, owner, _, *_ = exchange
    thread, _, request = start(exchange)
    target = request.with_name('stopped-exec-draft.json')
    command = host.draft_command(request, target, json.loads(draft.read_text()), owner=owner)
    environment = dict(os.environ, UV_OFFLINE='1')
    environment.pop('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', None)
    environment.pop('OA_FINANCE_PREAUDIT_STOP_FILE', None)
    try:
        runtime_control.request_stop()
        result = subprocess.run(shlex.split(command), env=environment,
            cwd=Path(host.__file__).resolve().parents[2], capture_output=True, text=True, timeout=15)
        assert result.returncode != 0
        assert 'stop_requested' in result.stderr
        assert not target.exists()
    finally:
        host.close(owner=owner, reason='cancelled')
        thread.join(3)
    assert not thread.is_alive()


@pytest.mark.parametrize('case', ['opaque', 'wrong_publication', 'malformed_opaque', 'wrong_plaintext', 'different_parent_file'])
def test_opaque_dispatch_requires_registered_parent_and_actual_child_request_receipt(exchange, case):
    import shutil
    action, draft, owner, child, task, _, engine = exchange
    results = []
    thread = threading.Thread(target=lambda: results.append(controller._review_only(task)))
    thread.start()
    request = wait_request(action, owner)
    assert request.exists()
    parent = child.with_name('parent.jsonl')
    message = 'gAAAA' + 'A' * 120 + '=='
    if case == 'malformed_opaque': message = 'gAAAA-not-a-native-record'
    if case == 'wrong_plaintext': message = 'Read a different request.'
    if case == 'different_parent_file':
        message = str(request)
        other = parent.with_name('copied-parent.jsonl')
        shutil.copyfile(parent, other)
        parent = other
    dispatch = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': message}, 'opaque-dispatch', '')
    try:
        if case in {'malformed_opaque', 'wrong_plaintext', 'different_parent_file'}:
            with pytest.raises(ValueError, match='source_untrusted'):
                host.assign(request, slot=0, owner=owner, source=parent, source_line=dispatch)
        else:
            assigned = host.assign(request, slot=0, owner=owner, source=parent, source_line=dispatch)
            assert assigned['dispatch_message_readable'] is False
            published_request = request
            if case == 'wrong_publication':
                another = Path(engine.next_action('general_expense')['action_file'])
                published_request = host.publish(another, prompt='Different frozen job.', timeout_seconds=60)
            line = draft_source(child, published_request, draft, owner)
            if case == 'wrong_publication':
                with pytest.raises(ValueError, match='source_untrusted'):
                    host.submit(request, draft, owner=owner, source=child, source_line=line)
            else:
                assert host.submit(request, draft, owner=owner, source=child, source_line=line)['status'] == 'published'
    finally:
        host.close(owner=owner, reason='cancelled')
        thread.join(3)
    assert not thread.is_alive()


@pytest.mark.parametrize('opaque', [False, True])
def test_one_native_dispatch_cannot_be_consumed_by_two_requests(exchange, opaque):
    action, draft, owner, child, task, _, engine = exchange
    results = []
    thread = threading.Thread(target=lambda: results.append(controller._review_only(task)))
    thread.start()
    request = wait_request(action, owner)
    another = Path(engine.next_action('general_expense')['action_file'])
    second = host.publish(another, prompt='Second frozen job.', timeout_seconds=60)
    parent = child.with_name('parent.jsonl')
    message = 'gAAAA' + 'A' * 120 + '==' if opaque else f'Read {request} and {second}'
    dispatch = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': message}, 'single-native-ack', '')
    try:
        first = host.assign(request, slot=0, owner=owner, source=parent, source_line=dispatch)
        assert host.assign(request, slot=0, owner=owner, source=parent, source_line=dispatch) == first
        line = draft_source(child, request, draft, owner)
        host.submit(request, draft, owner=owner, source=child, source_line=line)
        with pytest.raises(ValueError, match='dispatch_reused'):
            host.assign(second, slot=0, owner=owner, source=parent, source_line=dispatch)
    finally:
        host.close(owner=owner, reason='cancelled')
        thread.join(3)
    assert not thread.is_alive()


def test_app_small_marketing_object_uses_existing_normalizer_and_keeps_raw_source(exchange):
    _, _, owner, child, _, manifest, engine = exchange
    action = Path(engine.next_action('small_marketing_fee')['action_file'])
    host.approval_lifecycle.heartbeat(manifest['run_id'], manifest['controller_instance_id'], {action}, [], writer_active=False)
    packet = json.loads(action.with_name('review-packet.json').read_text())
    keys = [item['item_key'] for item in packet['items']]
    raw = {'overall': 'needs_human', 'items': {
        key: {'status': 'uncertain', 'conclusion': '自动读取材料不足，需财务核对原单。',
              'evidence_refs': [], 'missing_evidence': ['缺少审核材料']} for key in keys}}
    task = {'project_root': str(runtime_control.PROJECT_ROOT), 'action_file': str(action), 'timeout_seconds': 60}
    results = []
    thread = threading.Thread(target=lambda: results.append(controller._review_only(task)))
    thread.start()
    request = wait_request(action, owner)
    parent = child.with_name('parent.jsonl')
    dispatch = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request)}, 'small-object-dispatch', '')
    target = action.with_name('model-keyed-items.json')
    try:
        host.assign(request, slot=0, owner=owner, source=parent, source_line=dispatch)
        line = draft_source(child, request, target, owner, payload=raw)
        host.submit(request, target, owner=owner, source=child, source_line=line)
        reviewed = host.read_result(request)
        assert reviewed['status'] == 'reviewed'
        assert [item['item_key'] for item in reviewed['decision']['items']] == keys
        assert [{k: v for k, v in item.items() if k != 'item_key'} for item in reviewed['decision']['items']] == list(raw['items'].values())
        assert json.loads(target.read_text()) == raw
        consumer = []
        completed = host.complete(action, reviewed, completer=lambda path, value: consumer.append(value))
        assert consumer == [reviewed['decision']]
        assert completed['error_code'] == 'host_review_submit_incomplete'
    finally:
        host.close(owner=owner, reason='cancelled')
        thread.join(3)
    assert not thread.is_alive()

def start(exchange):
    action, draft, owner, source, task, _, _ = exchange
    result = []
    thread = threading.Thread(target=lambda: result.append(controller._review_only(task)))
    thread.start()
    request = wait_request(action, owner)
    assert request.exists()
    parent = source.with_name('parent.jsonl')
    dispatch = native_call(parent, 'collaboration.followup_task',
        {'target': '/root/reviewer_0', 'message': 'Read and audit ' + str(request)}, 'dispatch-0', {})
    host.assign(request, slot=0, owner=owner, source=parent, source_line=dispatch)
    draft_source(source, request, draft, owner)
    return thread, result, request


def test_public_controller_host_exchange_never_launches_cli_and_preserves_unknown_identity(exchange, monkeypatch):
    action, draft, owner, source, _, _, engine = exchange
    monkeypatch.setattr(worker, '_execute_codex', lambda *_: pytest.fail('CLI invoked'))
    monkeypatch.setattr(worker, '_codex_command', lambda *_a, **_k: pytest.fail('CLI command built'))
    thread, results, request = start(exchange)
    assert host.submit(request, draft, owner=owner, source=source, source_line=2)['write_state'] == 'not_requested'
    thread.join(3)
    assert not thread.is_alive()
    reviewed = results[0]
    assert reviewed['status'] == 'reviewed'
    assert reviewed['process_attempts'] == 0
    assert reviewed['codex_events']['counts'] == {}
    assert reviewed['token_usage'] == {'status': 'unknown'}
    assert reviewed['model'] == 'host-current-chat'
    submitted = []
    monkeypatch.setattr(worker, '_read_completed', lambda _: 'needs_human')
    complete = worker.complete_review_result(action, reviewed,
        completer=lambda path, decision: submitted.append((path, decision)))
    assert complete['status'] == 'completed' and len(submitted) == 1
    assert complete['write_state'] == 'submitted'
    # The existing FlowEngine consumer accepts the exact host-produced decision.
    engine.validate_review_decision(action, submitted[0][1])


@pytest.mark.parametrize('damage', ['owner', 'source', 'decision', 'frozen', 'stopped', 'run', 'expired'])
def test_submission_rejects_untrusted_exchange_without_publishing(exchange, monkeypatch, damage):
    action, draft, owner, source, _, manifest, _ = exchange
    thread, results, request = start(exchange)
    if damage == 'owner':
        owner = str(uuid4())
    elif damage == 'source':
        source.write_text(source.read_text().replace('actual-host-call', ''))
    elif damage == 'decision':
        value = json.loads(draft.read_text()); value['items'] = []; json_artifact.write_json_atomic(draft, value)
    elif damage == 'frozen':
        value = json.loads(action.with_name('material.json').read_text()); value['warnings'] = ['changed']
        json_artifact.write_json_atomic(action.with_name('material.json'), value)
    elif damage == 'stopped':
        monkeypatch.setattr(runtime_control, 'stop_requested', lambda: True)
    elif damage == 'run':
        original_manifest = dict(manifest)
        changed = {**manifest, 'run_id': uuid4().hex}
        host.guard.write(controller_handoff.manifest_file(), changed)
    else:
        value = json.loads(request.read_text()); value['expires_at'] = 0
        json_artifact.write_json_atomic(request, value)
    with pytest.raises((ValueError, KeyError)):
        host.submit(request, draft, owner=owner, source=source, source_line=2)
    if damage == 'run':
        host.guard.write(controller_handoff.manifest_file(), original_manifest)
    assert not request.with_name(request.name.replace(host.REQUEST, host.SUBMISSION)).exists()
    monkeypatch.setattr(runtime_control, 'stop_requested', lambda: True)
    thread.join(3)
    assert not thread.is_alive()
    assert results[0]['write_state'] == 'not_requested'


def test_submission_is_exclusive_and_completion_rejects_changed_source(exchange, monkeypatch):
    action, draft, owner, source, *_ = exchange
    thread, results, request = start(exchange)
    host.submit(request, draft, owner=owner, source=source, source_line=2)
    with pytest.raises((ValueError, RuntimeError), match='assignment_untrusted|control_file_unsafe'):
        host.submit(request, draft, owner=owner, source=source, source_line=2)
    thread.join(3)
    source.write_text(source.read_text().replace('actual-host-call', 'different-call'))
    stops = []
    monkeypatch.setattr(runtime_control, 'request_cycle_stop', lambda: stops.append(True))
    result = host.complete(action, results[0], completer=lambda *_: pytest.fail('untrusted write'))
    assert result['write_state'] == 'not_requested' and stops == [True]


def test_host_provider_is_not_silently_relabelled_as_deepseek(monkeypatch):
    monkeypatch.setenv(host.TRANSPORT_ENV, 'host_chat')
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'deepseek')
    with pytest.raises(ValueError, match='transport_invalid|provider_mismatch'):
        host.selected()
    with pytest.raises(ValueError, match='transport_invalid|provider_mismatch'):
        host.settings()


def test_host_mode_forbids_building_a_codex_command(monkeypatch):
    monkeypatch.setenv(host.TRANSPORT_ENV, 'host_chat')
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    with pytest.raises(ValueError, match='openai_review_cli_forbidden'):
        worker._codex_command(Path.cwd(), Path('schema'), Path('decision'))


def test_fresh_round_selects_host_and_next_round_resets_to_cli(monkeypatch, capsys):
    monkeypatch.setattr(finance_preaudit_run.auth, 'load_local_env', lambda: None)
    monkeypatch.setattr(finance_preaudit_run.approval_lifecycle, 'startup', lambda _: {'status': 'ready'})
    seen = []
    def supervise(*_, **__):
        seen.append(host.settings())
        return {'result': 'completed'}
    monkeypatch.setattr(finance_preaudit_run, 'supervise', supervise)
    for options in (['--review-transport', 'host_chat'], ['--review-provider', 'deepseek']):
        assert finance_preaudit_run.main(['--owner-task-id', str(uuid4()), *options, '--', '--rolling']) == 0
        capsys.readouterr()
    assert seen[0] == host.IDENTITY
    assert seen[1]['provider'] == 'deepseek' and 'transport' not in seen[1]


def test_same_registered_child_reviews_two_frozen_actions_without_recreation(exchange, monkeypatch):
    from tests.finance_preaudit.test_app_reviewer_slots import native_sessions
    action, draft, owner, source, task, manifest, engine = exchange
    thread, results, request = start(exchange)
    host.submit(request, draft, owner=owner, source=source, source_line=2)
    thread.join(3)
    reviewers = host.registered_reviewers(owner=owner)
    # A second workflow creates a distinct frozen action while the three reviewers remain unchanged.
    action2 = Path(engine.next_action('general_expense')['action_file'])
    packet = json.loads(action2.with_name('review-packet.json').read_text())
    draft2 = draft.with_name('second-draft.json')
    json_artifact.write_json_atomic(draft2, {'overall': 'needs_human', 'items': [
        {'item_key': item['item_key'], 'status': 'uncertain', 'conclusion': '自动读取材料不足，需财务核对原单。',
         'evidence_refs': [], 'missing_evidence': ['缺少审核材料']} for item in packet['items']]})
    draft2.chmod(0o600)
    host.approval_lifecycle.heartbeat(manifest['run_id'], manifest['controller_instance_id'], {action2}, [], writer_active=False)
    request2 = host.publish(action2, prompt='Review this frozen action only.', timeout_seconds=60)
    assert [item['request_file'] for item in host.pending(owner=owner)] == [str(request2)]
    parent = source.with_name('parent.jsonl')
    line = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request2)}, 'second-dispatch', {})
    host.assign(request2, slot=0, owner=owner, source=parent, source_line=line)
    source_line = draft_source(source, request2, draft2, owner, call_id='second-review')
    host.submit(request2, draft2, owner=owner, source=source, source_line=source_line)
    reviewed = host.await_result(request2)
    engine.validate_review_decision(action2, reviewed['decision'])
    assert host.registered_reviewers(owner=owner) == reviewers
    assert host.pending(owner=owner) == []


def test_parent_lease_loss_cancels_exchange_and_rejects_late_decision(exchange, monkeypatch):
    action, draft, owner, source, task, *_ = exchange
    task['timeout_seconds'] = 600
    thread, results, request = start(exchange)
    now = time.time()
    monkeypatch.setattr(host.time, 'time', lambda: now + 301)
    with pytest.raises(ValueError, match='parent_lost'):
        host.submit(request, draft, owner=owner, source=source, source_line=2)
    thread.join(3)
    assert not thread.is_alive()
    assert results[0]['write_state'] == 'not_requested'
    assert json.loads(host.registry_file(owner).read_text())['state'] == 'cancelled'
    assert not request.with_name(request.name.replace(host.REQUEST, host.SUBMISSION)).exists()


def test_cancel_closes_registered_round_and_never_terminates_writer(exchange, monkeypatch):
    action, draft, owner, source, *_ = exchange
    thread, results, request = start(exchange)
    monkeypatch.setattr(worker, '_terminate_process_group', lambda *_a, **_k: pytest.fail('Writer terminated'))
    assert host.close(owner=owner, reason='cancelled')['state'] == 'cancelled'
    with pytest.raises(ValueError):
        host.submit(request, draft, owner=owner, source=source, source_line=2)
    thread.join(3)
    assert not thread.is_alive() and results[0]['write_state'] == 'not_requested'
    assert not request.with_name(request.name.replace(host.REQUEST, host.SUBMISSION)).exists()


def seal_execution(owner, manifest):
    from datetime import datetime, timezone
    import hashlib
    value = json.loads(host.registry_file(owner).read_text())
    path = Path(value['original_execution']['receipt_file'])
    receipt = json.loads(path.read_text())
    terminal = controller_handoff._read_manifest()
    host.guard.write(path.with_name('controller-manifest.json'), terminal)
    logs = {}
    for name in ('stdout.log', 'stderr.log'):
        path.with_name(name).write_bytes(b'{}\n')
        path.with_name(name).chmod(0o600)
        logs[name] = hashlib.sha256(b'{}\n').hexdigest()
    receipt.update(state='exited', result='completed', exit_code=0, exit_signal=None,
        completed_at=datetime.now(timezone.utc).isoformat(), output_sha256=logs,
        run_id=manifest['run_id'], controller_instance_id=manifest['controller_instance_id'],
        controller_state='completed', children_reaped=True, code_revision=manifest['code_revision'],
        manifest_sha256=hashlib.sha256(path.with_name('controller-manifest.json').read_bytes()).hexdigest())
    host.guard.write(path, receipt)
    return path


def test_packaging_reuses_original_round_only_after_sealed_finance_end(exchange):
    action, _, owner, _, _, manifest, _ = exchange
    with pytest.raises(ValueError, match='finance_incomplete'):
        host.enter_packaging(owner=owner)
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    original = host.registered_reviewers(owner=owner)
    assert host.enter_packaging(owner=owner)['phase'] == 'packaging'
    assert host.registered_reviewers(owner=owner) == original
    draft_request = host.publish(action, prompt='Observe each package page.', timeout_seconds=60, kind='packaging_draft')
    confirmation = host.publish(action, prompt='Independent targeted review.', timeout_seconds=60,
        kind='packaging_confirmation', review_context={'draft_sha256': 'a' * 64})
    assert draft_request != confirmation
    assert json.loads(confirmation.read_text())['review_context']['draft_sha256'] == 'a' * 64
    assert {item['request_file'] for item in host.pending(owner=owner)} == {str(draft_request), str(confirmation)}


def test_app_parent_survives_a_native_dispatch_longer_than_45_seconds(exchange, monkeypatch):
    _, _, owner, child, *_ = exchange
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'functions.exec', 'heartbeat ' + owner, 'before-slow-native-dispatch', {'exit_code': 0})
    initial = host.heartbeat(owner=owner, source=parent, source_line=line)
    now = initial['heartbeat_at']
    monkeypatch.setattr(host.time, 'time', lambda: now + 45.179)
    assert len(host.registered_reviewers(owner=owner)) == 3
    assert not runtime_control.stop_requested()


@pytest.mark.parametrize('elapsed', [299.999, 300.0])
def test_app_parent_lease_has_a_hard_five_minute_deadline(exchange, monkeypatch, elapsed):
    _, _, owner, child, *_ = exchange
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'functions.exec', 'heartbeat ' + owner, 'bounded-parent-lease', {'exit_code': 0})
    initial = host.heartbeat(owner=owner, source=parent, source_line=line)
    monkeypatch.setattr(host.time, 'time', lambda: initial['heartbeat_at'] + elapsed)
    if elapsed < 300:
        assert len(host.registered_reviewers(owner=owner)) == 3
    else:
        with pytest.raises(ValueError, match='parent_lost'):
            host.registered_reviewers(owner=owner)
        with pytest.raises(ValueError, match='parent_lost'):
            host.heartbeat(owner=owner, source=parent, source_line=line)


def test_app_parent_enters_packaging_after_a_long_native_dispatch(exchange, monkeypatch):
    _, _, owner, child, _, manifest, _ = exchange
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'functions.exec', 'heartbeat ' + owner, 'finance-before-native-delay', {'exit_code': 0})
    initial = host.heartbeat(owner=owner, source=parent, source_line=line)
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    monkeypatch.setattr(host.time, 'time', lambda: initial['heartbeat_at'] + 45.179)
    assert host.enter_packaging(owner=owner)['phase'] == 'packaging'
    assert len(host.registered_reviewers(owner=owner)) == 3


def test_only_fresh_parent_native_call_can_renew_round_lease(exchange):
    _, _, owner, child, *_ = exchange
    parent = child.with_name('parent.jsonl')
    child_line = native_call(child, 'functions.exec', 'heartbeat ' + owner, 'child-heartbeat', {'exit_code': 0})
    with pytest.raises(ValueError, match='source_untrusted'):
        host.heartbeat(owner=owner, source=child, source_line=child_line)
    parent_line = native_call(parent, 'functions.exec', 'heartbeat ' + owner, 'parent-heartbeat', {'exit_code': 0})
    first = host.heartbeat(owner=owner, source=parent, source_line=parent_line)
    assert first['state'] == 'open'
    repeated = host.heartbeat(owner=owner, source=parent, source_line=parent_line)
    assert repeated['heartbeat_at'] == first['heartbeat_at']


def test_normal_close_requires_original_finance_terminal_and_no_pending_work(exchange):
    action, _, owner, child, _, manifest, _ = exchange
    with pytest.raises(ValueError, match='close_incomplete'):
        host.close(owner=owner)
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    host.enter_packaging(owner=owner)
    host.publish(action, prompt='Review package draft.', timeout_seconds=60, kind='packaging_draft')
    with pytest.raises(ValueError, match='close_incomplete'):
        host.close(owner=owner)


def test_normal_close_proves_all_three_native_reviewers_are_idle(exchange):
    _, _, owner, child, _, manifest, _ = exchange
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'list_agents', {}, 'native-status', {'agents': [
        {'agent_name': f'/root/reviewer_{slot}', 'agent_status': 'completed'} for slot in range(3)]})
    assert host.close(owner=owner, source=parent, source_line=line)['state'] == 'closed'


def test_read_result_revalidates_original_child_decision_and_keeps_stable_receipt(exchange):
    _, draft, owner, source, *_ = exchange
    thread, results, request = start(exchange)
    host.submit(request, draft, owner=owner, source=source, source_line=2)
    thread.join(3)
    first = host.read_result(request)
    assert host.read_result(request) == first
    assert first['host_receipt'] == results[0]['host_receipt']
    json_artifact.write_json_atomic(draft, {'overall': 'pass', 'items': []})
    draft.chmod(0o600)
    with pytest.raises(ValueError, match='source_untrusted'):
        host.read_result(request)


@pytest.mark.parametrize('damage', ['cross_slot', 'failed_output', 'missing_output', 'bare_exec'])
def test_child_tool_source_must_match_assigned_native_slot_and_success(exchange, monkeypatch, damage):
    action, draft, owner, source, *_ = exchange
    thread, results, request = start(exchange)
    line = 2
    if damage == 'cross_slot':
        source = source.with_name('child-1.jsonl')
        line = draft_source(source, request, draft, owner, call_id='cross-slot-call')
    elif damage == 'failed_output':
        rows = source.read_text().splitlines()
        row = json.loads(rows[2])
        output = json.loads(row['payload']['output'])
        output['exit_code'] = 2
        row['payload']['output'] = json.dumps(output)
        rows[2] = json.dumps(row)
        source.write_text('\n'.join(rows) + '\n')
    elif damage == 'missing_output':
        source.write_text(''.join(source.read_text().splitlines(keepends=True)[:2]))
    else:
        source.write_text(source.read_text().replace('functions.exec', 'exec'))
    if damage == 'bare_exec':
        assert host.submit(request, draft, owner=owner, source=source, source_line=line)['status'] == 'published'
    else:
        with pytest.raises(ValueError, match='source_untrusted'):
            host.submit(request, draft, owner=owner, source=source, source_line=line)
        assert not request.with_name(request.name.replace(host.REQUEST, host.SUBMISSION)).exists()
        host.close(owner=owner, reason='cancelled')
    thread.join(3)
    assert not thread.is_alive()


def test_fixed_slot_cannot_overlap_or_change_its_assignment(exchange):
    action, draft, owner, source, _, _, engine = exchange
    thread, results, request = start(exchange)
    action2 = Path(engine.next_action('general_expense')['action_file'])
    request2 = host.publish(action2, prompt='Review second action.', timeout_seconds=60)
    parent = source.with_name('parent.jsonl')
    dispatch = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request2)}, 'busy-dispatch', {})
    with pytest.raises(ValueError, match='slot_busy'):
        host.assign(request2, slot=0, owner=owner, source=parent, source_line=dispatch)
    line = native_call(parent, 'followup_task', {'target': '/root/reviewer_1', 'message': str(request)}, 'rebind-dispatch', {})
    with pytest.raises(ValueError, match='assignment_rebinding'):
        host.assign(request, slot=1, owner=owner, source=parent, source_line=line)
    host.close(owner=owner, reason='cancelled')
    thread.join(3)
    assert not thread.is_alive()


def test_public_heartbeat_command_can_discover_its_already_logged_parent_anchor(exchange, capsys):
    _, _, owner, child, *_ = exchange
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'functions.exec', 'heartbeat ' + owner, 'current-heartbeat', {'exit_code': 0})
    anchor = host.source_anchor(owner=owner)
    assert anchor == {'source': parent, 'source_line': line}
    assert host.main(['--operation', 'heartbeat', '--owner-task-id', owner,
        '--source-session-file', str(anchor['source']), '--source-line', str(anchor['source_line'])]) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'open'


@pytest.mark.parametrize('exchange', [False], indirect=True)
def test_first_review_waits_for_independent_parent_heartbeat_after_registration(exchange, request):
    action, draft, owner, child, task, *_ = exchange
    results = []
    thread = threading.Thread(target=lambda: results.append(host.review(task)))
    parent = child.with_name('parent.jsonl')
    thread.start()
    try:
        time.sleep(.05)
        assert thread.is_alive() and not results and not action.with_name(host.REQUEST).exists()
        same_call = native_call(parent, 'functions.exec', 'register then heartbeat ' + owner, 'combined-call', {})
        host.register(request.node.native_reviewers, owner=owner, source=parent, **request.node.original_binding)
        with pytest.raises(ValueError, match='host_review_stale_source'):
            host.heartbeat(owner=owner, source=parent, source_line=same_call)
        with pytest.raises(AssertionError, match='Current round did not publish'):
            wait_request(action, owner, timeout=.5)
        assert thread.is_alive() and not results
        independent = native_call(parent, 'functions.exec', 'heartbeat ' + owner, 'independent-first-heartbeat', {})
        host.heartbeat(owner=owner, source=parent, source_line=independent)
        published = wait_request(action, owner)
        dispatch = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(published)}, 'boot-dispatch', {})
        host.assign(published, slot=0, owner=owner, source=parent, source_line=dispatch)
        source_line = draft_source(child, published, draft, owner, call_id='boot-review')
        host.submit(published, draft, owner=owner, source=child, source_line=source_line)
        thread.join(3)
        assert not thread.is_alive() and results[0]['status'] == 'reviewed'
    finally:
        try:
            host.close(owner=owner, reason='cancelled')
        finally:
            runtime_control.request_cycle_stop()
            thread.join(3)
            assert not thread.is_alive()


def test_parent_lease_observation_stops_admission_during_prepare_without_review_waiter(exchange, monkeypatch):
    _, _, owner, _, _, manifest, _ = exchange
    # Preparing has no published App review request or waiting review thread.
    assert host.pending(owner=owner) == []
    now = time.time()
    monkeypatch.setattr(host.time, 'time', lambda: now + 301)
    monkeypatch.setattr(worker, '_terminate_process_group', lambda *_a, **_k: pytest.fail('Writer killed'))
    with pytest.raises(ValueError, match='parent_lost'):
        host.assert_round_alive(owner=owner, manifest=manifest)
    assert runtime_control.stop_requested()
    assert json.loads(host.registry_file(owner).read_text())['state'] == 'cancelled'


@pytest.mark.parametrize('exchange', [False], indirect=True)
def test_bootstrap_observation_grace_is_bounded_by_the_original_manifest_clock(exchange):
    from datetime import datetime, timedelta, timezone
    _, _, owner, _, _, manifest, _ = exchange
    host.assert_round_alive(owner=owner, manifest=manifest)
    assert not runtime_control.stop_requested()
    original = dict(manifest)
    expired = {**manifest, 'started_at': (datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat()}
    host.guard.write(controller_handoff.manifest_file(), expired)
    try:
        with pytest.raises(ValueError, match='parent_lost'):
            host.assert_round_alive(owner=owner, manifest=expired)
        assert runtime_control.stop_requested()
    finally:
        host.guard.write(controller_handoff.manifest_file(), original)


def test_round_observer_cannot_accept_a_different_controller_instance(exchange):
    _, _, owner, _, _, manifest, _ = exchange
    changed = {**manifest, 'controller_instance_id': uuid4().hex}
    with pytest.raises(ValueError, match='parent_lost'):
        host.assert_round_alive(owner=owner, manifest=changed)
    assert runtime_control.stop_requested()


def test_assignment_accepts_actual_paired_empty_native_followup_acknowledgement(exchange):
    action, _, owner, child, *_ = exchange
    request = host.publish(action, prompt='Review assigned frozen action.', timeout_seconds=60)
    parent = child.with_name('parent.jsonl')
    source_line = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request)},
        'actual-empty-native-ack', '')
    assigned = host.assign(request, slot=0, owner=owner, source=parent, source_line=source_line)
    assert assigned['state'] == 'assigned'
    assert assigned['dispatch_source']['output'] == {}
    host.close(owner=owner, reason='cancelled')


def test_normal_close_accepts_actual_native_completed_status_objects_without_retaining_results(exchange):
    _, _, owner, child, _, manifest, _ = exchange
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'list_agents', {}, 'actual-native-status', {'agents': [
        {'agent_name': f'/root/reviewer_{slot}', 'agent_status': {'completed': 'private reviewer result'}}
        for slot in range(3)] + [{'agent_name': '/root/other', 'agent_status': {'completed': 'unrelated private result'}}]})
    assert host.close(owner=owner, source=parent, source_line=line)['state'] == 'closed'
    stored = host.registry_file(owner).read_text()
    assert 'private reviewer result' not in stored and 'unrelated private result' not in stored
    proof = json.loads(stored)['children_idle_source']
    assert proof['reviewers'] == [{'agent_name': f'/root/reviewer_{slot}', 'agent_status': 'completed'} for slot in range(3)]


def test_registry_read_rejects_heartbeat_metadata_that_no_longer_matches_parent_source(exchange):
    _, _, owner, child, *_ = exchange
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'exec', 'heartbeat ' + owner, 'actual-heartbeat-proof', {'exit_code': 0})
    host.heartbeat(owner=owner, source=parent, source_line=line)
    time.sleep(.005)
    value = json.loads(host.registry_file(owner).read_text())
    value['heartbeat_at'] += .001
    host.guard.write(host.registry_file(owner), value)
    with pytest.raises(ValueError, match='heartbeat_untrusted'):
        host.registered_reviewers(owner=owner)


@pytest.mark.parametrize('status', [{'completed': 'done', 'running': None}, {'completed': 42}, 'running'])
def test_normal_close_rejects_malformed_or_active_native_status(exchange, status):
    _, _, owner, child, _, manifest, _ = exchange
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'list_agents', {}, 'invalid-native-status', {'agents': [
        {'agent_name': '/root/reviewer_0', 'agent_status': status},
        {'agent_name': '/root/reviewer_1', 'agent_status': {'completed': 'done'}},
        {'agent_name': '/root/reviewer_2', 'agent_status': 'idle'}]})
    with pytest.raises(ValueError, match='children_status_untrusted|children_not_idle'):
        host.close(owner=owner, source=parent, source_line=line)


def test_child_decision_bytes_cannot_be_replaced_after_the_completed_native_write(exchange):
    _, draft, owner, child, *_ = exchange
    thread, results, request = start(exchange)
    payload = json.loads(draft.read_text())
    payload['items'][0]['conclusion'] = '父调用者替换后的有效决定'
    host.guard.write(draft, payload)
    with pytest.raises(ValueError, match='source_untrusted'):
        host.submit(request, draft, owner=owner, source=child, source_line=2)
    host.close(owner=owner, reason='cancelled')
    thread.join(3)
    assert not thread.is_alive()


@pytest.mark.parametrize('damage', ['commented_command', 'unrelated_success', 'mixed_failure', 'actual_output_list'])
def test_native_publisher_requires_executable_command_and_its_exact_receipt(exchange, damage):
    _, draft, owner, child, *_ = exchange
    thread, results, request = start(exchange)
    rows = child.read_text().splitlines()
    call, output = json.loads(rows[1]), json.loads(rows[2])
    shell = json.loads(output['payload']['output'])
    if damage == 'commented_command':
        call['payload']['input'] = '// ' + call['payload']['input'] + '\ntext(await tools.exec_command({"cmd":"true"}));'
    elif damage == 'unrelated_success':
        output['payload']['output'] = json.dumps({'exit_code': 0, 'output': '{}'})
    elif damage == 'mixed_failure':
        output['payload']['output'] = json.dumps({'ok': shell, 'failed': {'exit_code': 1, 'output': 'failed'}})
    else:
        output['payload']['output'] = json.dumps([
            {'type': 'input_text', 'text': 'Script completed\nWall time 0.1 seconds\nOutput:'},
            {'type': 'input_text', 'text': json.dumps(shell)}])
    rows[1], rows[2] = json.dumps(call), json.dumps(output)
    child.write_text('\n'.join(rows) + '\n')
    try:
        if damage == 'actual_output_list':
            assert host.submit(request, draft, owner=owner, source=child, source_line=2)['status'] == 'published'
        else:
            with pytest.raises(ValueError, match='source_untrusted'):
                host.submit(request, draft, owner=owner, source=child, source_line=2)
    finally:
        if damage != 'actual_output_list':
            host.close(owner=owner, reason='cancelled')
        thread.join(3)
    assert not thread.is_alive()


def test_public_draft_publisher_command_returns_exact_exclusive_byte_receipt(exchange, capsys):
    action, draft, owner, child, *_ = exchange
    request = host.publish(action, prompt='Read current frozen facts.', timeout_seconds=60)
    payload = json.loads(draft.read_text())
    target = draft.with_name('exclusive-published.json')
    args = ['--operation', 'write-draft', '--owner-task-id', owner, '--request-file', str(request),
            '--decision-file', str(target), '--payload-json', json.dumps(payload, ensure_ascii=False)]
    assert host.main(args) == 0
    receipt = json.loads(capsys.readouterr().out)
    import hashlib
    assert receipt['decision_sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert json.loads(target.read_text()) == payload and target.stat().st_mode & 0o777 == 0o600
    with pytest.raises((FileExistsError, RuntimeError)):
        host.main(args)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == receipt['decision_sha256']
