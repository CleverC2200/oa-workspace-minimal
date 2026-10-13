from __future__ import annotations

import json
from uuid import uuid4

import pytest

from apps.oa_finance_preaudit import host_chat_review as host


def native_sessions(tmp_path, monkeypatch):
    home = tmp_path / 'codex'
    sessions = home / 'sessions'
    sessions.mkdir(parents=True)
    monkeypatch.setenv('CODEX_HOME', str(home))
    owner = str(uuid4())
    parent = sessions / 'parent.jsonl'
    lines = [{'type': 'session_meta', 'payload': {'id': owner}}]
    reviewers = []
    for slot in range(3):
        agent_path = f'/root/reviewer_{slot}'
        call = f'spawn-{slot}'
        spawn_line = len(lines) + 1
        lines.extend([
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'spawn_agent',
             'call_id': call, 'arguments': json.dumps({'task_name': f'reviewer_{slot}', 'message': 'Review only assigned frozen materials.'})}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': call,
             'output': json.dumps({'task_name': agent_path})}},
        ])
        child = sessions / f'child-{slot}.jsonl'
        child.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': str(uuid4()),
            'source': {'subagent': {'thread_spawn': {'parent_thread_id': owner, 'depth': 1, 'agent_path': agent_path}}}}}) + '\n')
        child.chmod(0o600)
        reviewers.append({'session_file': str(child), 'spawn_line': spawn_line})
    parent.write_text(''.join(json.dumps(line) + '\n' for line in lines))
    parent.chmod(0o600)
    return owner, parent, reviewers


def test_registry_rejects_a_child_without_native_parent_spawn_ancestry(tmp_path, monkeypatch):
    owner, parent, reviewers = native_sessions(tmp_path, monkeypatch)
    child = __import__('pathlib').Path(reviewers[0]['session_file'])
    value = json.loads(child.read_text())
    value['payload']['source']['subagent']['thread_spawn']['parent_thread_id'] = str(uuid4())
    child.write_text(json.dumps(value) + '\n')
    with pytest.raises(ValueError, match='host_review_source_untrusted'):
        host.register(reviewers, owner=owner, source=parent)



def original_execution(owner, manifest):
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    import os
    from apps.oa_finance_preaudit import approval_guard, controller_handoff
    from scripts import finance_preaudit_run
    invocation = uuid4().hex
    receipt_file = controller_handoff.manifest_file().parent / 'executions' / invocation / 'execution.json'
    receipt = {'schema_version': 1, 'kind': 'preaudit_controller_execution', 'owner_task_id': owner,
        'invocation_id': invocation, 'state': 'waiting', 'controller_pid': os.getpid(), 'process_group_id': os.getpid(),
        'supervisor_pid': os.getpid(), 'supervisor_process': approval_guard.process_identity(os.getpid()),
        'controller_process': approval_guard.process_identity(os.getpid()), 'children_reaped': 'unknown',
        'started_at': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), 'completed_at': None,
        'review_configuration': host.IDENTITY, 'exit_code': None}
    approval_guard.write(receipt_file, receipt)
    approval_guard.write(receipt_file.with_name('controller-manifest.json'), manifest)
    finance_preaudit_run.bind_tool_session(receipt_file, owner_task_id=owner, session_id='original-session')
    return {'receipt_file': receipt_file, 'invocation_id': invocation, 'tool_session_id': 'original-session'}

def test_registry_keeps_exactly_three_fixed_native_conversations(tmp_path, monkeypatch):
    from apps.oa_approval import runtime_control
    from apps.oa_finance_preaudit import controller_handoff, approval_guard
    import os
    owner, parent, reviewers = native_sessions(tmp_path, monkeypatch)
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'control/controller.lock'))
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    monkeypatch.setenv(host.TRANSPORT_ENV, 'host_chat')
    identity = approval_guard.process_identity(os.getpid())
    monkeypatch.setenv(approval_guard.EXECUTION_OWNERS_ENV, json.dumps([identity]))
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({'review_configuration': host.IDENTITY})
        binding = original_execution(owner, manifest)
        monkeypatch.delenv(approval_guard.EXECUTION_OWNERS_ENV, raising=False)
        registered = host.register(reviewers, owner=owner, source=parent, **binding)
        assert registered['run_id'] == manifest['run_id']
        assert [item['slot'] for item in registered['reviewers']] == [0, 1, 2]
        assert host.register(reviewers, owner=owner, source=parent, **binding)['reviewers'] == registered['reviewers']
        with pytest.raises(ValueError, match='fixed_slots'):
            host.register(reviewers[:2], owner=owner, source=parent)
        changed_owner, changed_parent, changed_reviewers = native_sessions(tmp_path / 'new', monkeypatch)
        # A new native child cannot replace an existing reviewer in the same round.
        with pytest.raises(ValueError):
            host.register(changed_reviewers, owner=owner, source=changed_parent)
        controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
