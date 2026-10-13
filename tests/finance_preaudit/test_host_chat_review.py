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
def exchange(tmp_path, monkeypatch):
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
    owner = str(uuid4())
    run, instance = uuid4().hex, uuid4().hex
    identity = {'pid': 1234, 'pgid': 1234, 'birth': 'offline', 'uid': os.getuid()}
    manifest = {'state': 'running', 'controller_pid': 1234, 'run_id': run,
                'controller_instance_id': instance, 'review_configuration': host.IDENTITY}
    monkeypatch.setenv(host.TRANSPORT_ENV, 'host_chat')
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    monkeypatch.setenv(host.guard.OWNER_ENV, owner)
    monkeypatch.setattr(host.approval_lifecycle, 'context', lambda: (run, instance, str(tmp_path)))
    monkeypatch.setattr(controller_handoff, '_read_manifest', lambda: manifest)
    monkeypatch.setattr(runtime_control, 'PROJECT_ROOT', root)
    monkeypatch.setattr(runtime_control, 'stop_requested', lambda: False)
    monkeypatch.setattr(runtime_control, 'controller_owner_pid', lambda: 1234)
    monkeypatch.setattr(host.guard, 'process_identity', lambda _: identity)
    monkeypatch.setattr(host.guard, 'execution_owners_alive', lambda: True)
    home = tmp_path / 'codex'
    monkeypatch.setenv('CODEX_HOME', str(home))
    source = home / 'sessions' / 'session.jsonl'
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': owner}}) + '\n' +
        json.dumps({'type': 'response_item', 'payload': {'type': 'custom_tool_call',
            'name': 'functions.exec', 'call_id': 'actual-host-call', 'input': 'write ' + str(draft)}}) + '\n')
    source.chmod(0o600)
    task = {'project_root': str(root), 'action_file': str(action), 'timeout_seconds': 60}
    return action, draft, owner, source, task, manifest, engine


def start(exchange):
    action, _, _, _, task, _, _ = exchange
    result = []
    thread = threading.Thread(target=lambda: result.append(controller._review_only(task)))
    thread.start()
    deadline = time.monotonic() + 3
    request = action.with_name(host.REQUEST)
    while not request.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert request.exists()
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
        manifest['run_id'] = uuid4().hex
    else:
        value = json.loads(request.read_text()); value['expires_at'] = 0
        json_artifact.write_json_atomic(request, value)
    with pytest.raises((ValueError, KeyError)):
        host.submit(request, draft, owner=owner, source=source, source_line=2)
    assert not action.with_name(host.SUBMISSION).exists()
    monkeypatch.setattr(runtime_control, 'stop_requested', lambda: True)
    thread.join(3)
    assert not thread.is_alive()
    assert results[0]['write_state'] == 'not_requested'


def test_submission_is_exclusive_and_completion_rejects_changed_source(exchange, monkeypatch):
    action, draft, owner, source, *_ = exchange
    thread, results, request = start(exchange)
    host.submit(request, draft, owner=owner, source=source, source_line=2)
    with pytest.raises(RuntimeError, match='control_file_unsafe'):
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
    with pytest.raises(ValueError, match='provider_mismatch'):
        host.selected()
    with pytest.raises(ValueError, match='provider_mismatch'):
        host.settings()


def test_host_mode_forbids_building_a_codex_command(monkeypatch):
    monkeypatch.setenv(host.TRANSPORT_ENV, 'host_chat')
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    with pytest.raises(ValueError, match='host_review_cli_forbidden'):
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
