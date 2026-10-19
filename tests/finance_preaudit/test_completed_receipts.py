from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import cli, controller_handoff
from apps.oa_finance_preaudit import app_review_shutdown as shutdown
from tests.finance_preaudit.test_app_review_shutdown import proofs
from tests.finance_preaudit.test_host_chat_review import exchange, seal_execution


def test_cli_summarizes_only_explicit_sealed_round_without_writes(exchange, tmp_path, capsys):
    action, _, owner, child, _, manifest, _ = exchange
    shutdown.begin(owner=owner, reason='completed')
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    shutdown.finish(owner=owner, **proofs(owner, child))
    root = action.parents[4]
    original = {file: file.read_bytes() for file in tmp_path.rglob('*') if file.is_file()}
    output = tmp_path / 'report.json'
    assert cli.entrypoint(['completed-receipts', '--root', str(root), '--owner-task-id', owner,
                           '--run-id', manifest['run_id'], '--output-file', str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['counts'] == {'completed': 0, 'excluded': 0, 'invalid': 0, 'duplicate': 0}
    assert result['business_acceptance'] == 'not_verified'
    assert 'details' not in result
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())['rounds'][0]['run_id'] == manifest['run_id']
    assert all(file.read_bytes() == raw for file, raw in original.items())
    assert {file for file in tmp_path.rglob('*') if file.is_file()} - set(original) == {output}


def completed_case(exchange, tmp_path, *, controlled=True, legacy=False):
    import hashlib
    from uuid import uuid4
    from apps import json_artifact
    from apps.oa_finance_preaudit import host_chat_review as host
    from tests.finance_preaudit.test_flow_engine_support import _engine, _decision, _reviewed_items
    action, _, owner, child, _, manifest, _ = exchange
    root = action.parents[4]
    engine = _engine(root, controlled=controlled)
    opened = engine.next_action('business_support_fee', mode='controlled_write' if controlled else 'preview')
    current = Path(opened['action_file'])
    if legacy:
        state_file = current.with_name('state.json')
        state = json.loads(state_file.read_text())
        state.pop('action_reference_sha256')
        json_artifact.write_json_atomic(current, opened)
        json_artifact.write_json_atomic(state_file, state)
    fingerprints = host._fingerprints(current)
    decision = tmp_path / 'completed-decision.json'
    json_artifact.write_json_atomic(decision, _decision(opened, _reviewed_items(['pass'] * 4)))
    engine.complete_review(current, decision)
    shutdown.begin(owner=owner, reason='completed')
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    shutdown.finish(owner=owner, **proofs(owner, child))
    registry_file = host.registry_file(owner, manifest['run_id'])
    registry = json.loads(registry_file.read_text())
    request_id = uuid4().hex
    request_file = host._request_file(current, 'finance', owner=owner, run_id=manifest['run_id'])
    request = {'schema_version': 1, 'kind': 'preaudit_host_review_request', 'review_kind': 'finance',
               'request_id': request_id, 'owner_task_id': owner, 'run_id': manifest['run_id'],
               'controller_instance_id': manifest['controller_instance_id'], 'action_file': str(current),
               'input_fingerprints': fingerprints}
    host.guard.write(request_file, request)
    registry['requests'] = {request_id: {'request_file': str(request_file),
                           'request_sha256': hashlib.sha256(request_file.read_bytes()).hexdigest()}}
    host.guard.write(registry_file, registry)
    return root, owner, manifest['run_id'], current, registry_file, request_file


@pytest.mark.parametrize('fingerprint', ['material.json', 'action_token_sha256'])
def test_cli_rejects_request_bound_to_different_frozen_inputs(exchange, tmp_path, capsys, fingerprint):
    import hashlib
    from apps.oa_finance_preaudit import host_chat_review as host
    root, owner, run, _, registry_file, request_file = completed_case(exchange, tmp_path)
    request = json.loads(request_file.read_text())
    request['input_fingerprints'][fingerprint] = '0' * 64
    host.guard.write(request_file, request)
    registry = json.loads(registry_file.read_text())
    registry['requests'][request['request_id']]['request_sha256'] = hashlib.sha256(request_file.read_bytes()).hexdigest()
    host.guard.write(registry_file, registry)
    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner, '--run-id', run]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['counts']['completed'] == 0
    assert result['counts']['invalid'] == 1


@pytest.mark.parametrize('controlled', [True, False])
def test_cli_counts_submissions_separately_from_preview_and_business_acceptance(exchange, tmp_path, capsys, controlled):
    root, owner, run, _, _, _ = completed_case(exchange, tmp_path, controlled=controlled)
    original = {file: file.read_bytes() for file in tmp_path.rglob('*') if file.is_file()}
    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner,
                     '--run-id', run, '--run-id', run]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['counts'] == {'completed': int(controlled), 'excluded': int(not controlled), 'invalid': 0, 'duplicate': 0}
    assert result['business_acceptance'] == 'not_verified'
    assert result['selected_rounds'] == 1
    assert all(file.read_bytes() == raw for file, raw in original.items())


@pytest.mark.parametrize('damage', ['receipt_missing', 'receipt_body', 'fourflags', 'decision', 'material',
                                  'state_missing_workflow', 'state_not_closed', 'action_binding',
                                  'shutdown_missing', 'shutdown_digest', 'native_source', 'request_missing'])
def test_cli_never_counts_missing_or_changed_completion_evidence(exchange, tmp_path, capsys, damage):
    from apps.oa_finance_preaudit import host_chat_review as host
    root, owner, run, action, registry_file, request_file = completed_case(exchange, tmp_path)
    if damage == 'receipt_missing':
        action.with_name('execution-receipt.json').unlink()
    elif damage in {'receipt_body', 'fourflags'}:
        path = action.with_name('execution-receipt.json')
        payload = json.loads(path.read_text())
        payload['done_hit' if damage == 'fourflags' else 'material_fingerprint'] = False if damage == 'fourflags' else '0' * 64
        host.guard.write(path, payload)
    elif damage in {'decision', 'material'}:
        path = action.with_name('review-decision.json' if damage == 'decision' else 'material.json')
        path.write_text('{}')
    elif damage.startswith('state_'):
        path = action.with_name('state.json')
        payload = json.loads(path.read_text())
        if damage == 'state_missing_workflow':
            payload.pop('workflow_key')
        else:
            payload['execution_state'] = 'review_required'
        host.guard.write(path, payload)
    elif damage == 'action_binding':
        payload = json.loads(action.read_text())
        payload['attempt'] = 7
        host.guard.write(action, payload)
    elif damage.startswith('shutdown_'):
        payload = json.loads(registry_file.read_text())
        if damage == 'shutdown_missing':
            payload.pop('shutdown')
        else:
            payload['shutdown']['receipt']['execution_sha256'] = '0' * 64
        host.guard.write(registry_file, payload)
    elif damage == 'native_source':
        payload = json.loads(registry_file.read_text())
        source = Path(payload['shutdown']['receipt']['children_source']['session_file'])
        rows = source.read_text().splitlines()
        index = payload['shutdown']['receipt']['children_source']['output_line'] - 1
        row = json.loads(rows[index])
        result = json.loads(row['payload']['output'])
        result['agents'][0]['agent_status'] = 'running'
        row['payload']['output'] = json.dumps(result)
        rows[index] = json.dumps(row)
        source.write_text('\n'.join(rows) + '\n')
    else:
        request_file.unlink()
    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner, '--run-id', run]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['counts']['completed'] == 0
    assert result['counts']['invalid'] + result['counts']['excluded'] == 1
    assert len(json.dumps(result)) < 1500


@pytest.mark.parametrize('path_type', ['existing', 'symlink', 'parent_symlink'])
def test_cli_report_output_preserves_existing_files_and_rejects_symlinks(exchange, tmp_path, capsys, path_type):
    root, owner, run, _, _, _ = completed_case(exchange, tmp_path)
    existing = tmp_path / 'existing.json'
    existing.write_text('keep original')
    target = existing
    if path_type == 'symlink':
        target = tmp_path / 'link.json'
        target.symlink_to(existing)
    elif path_type == 'parent_symlink':
        parent = tmp_path / 'linked-directory'
        parent.symlink_to(tmp_path, target_is_directory=True)
        target = parent / 'new.json'
    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner,
                     '--run-id', run, '--output-file', str(target)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert 'counts' not in result
    assert existing.read_text() == 'keep original'
    assert not (tmp_path / 'new.json').exists()


@pytest.mark.parametrize('owner,run', [('invalid', '0' * 32), ('00000000-0000-0000-0000-000000000000', '../bad')])
def test_cli_rejects_unsafe_round_selection(tmp_path, capsys, owner, run):
    assert cli.main(['completed-receipts', '--root', str(tmp_path), '--owner-task-id', owner, '--run-id', run]) == 1
    assert json.loads(capsys.readouterr().out)['failure']['code'] == 'completed_receipts_selection_invalid'


def test_cli_keeps_supported_legacy_action_and_state_layout_read_only(exchange, tmp_path, capsys):
    root, owner, run, _, _, _ = completed_case(exchange, tmp_path, legacy=True)
    original = {file: file.read_bytes() for file in tmp_path.rglob('*') if file.is_file()}
    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner, '--run-id', run]) == 0
    assert json.loads(capsys.readouterr().out)['counts']['completed'] == 1
    assert all(file.read_bytes() == raw for file, raw in original.items())


def test_cli_deduplicates_same_action_across_independently_sealed_rounds(exchange, tmp_path, monkeypatch, capsys):
    import hashlib
    import os
    from uuid import uuid4
    from apps.oa_finance_preaudit import host_chat_review as host
    from tests.finance_preaudit.test_app_reviewer_slots import original_execution
    from tests.finance_preaudit.test_host_chat_review import native_call
    root, owner, first_run, action, _, original_request = completed_case(exchange, tmp_path)
    original = json.loads(original_request.read_text())
    parent = Path(exchange[3]).with_name('parent.jsonl')
    reviewers = []
    for slot in range(3):
        line = native_call(parent, 'spawn_agent', {'task_name': f'reviewer_{slot}', 'message': 'Read frozen materials.'},
                           f'next-spawn-{slot}', {'task_name': f'/root/reviewer_{slot}'})
        child = parent.with_name(f'next-child-{slot}.jsonl')
        child.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': str(uuid4()), 'source': {'subagent': {
            'thread_spawn': {'parent_thread_id': owner, 'depth': 1, 'agent_path': f'/root/reviewer_{slot}'}}}}}) + '\n')
        child.chmod(0o600)
        reviewers.append({'session_file': str(child), 'spawn_line': line})
    monkeypatch.setattr(controller_handoff, 'CONTROLLER_INSTANCE_ID', uuid4().hex)
    manifest = controller_handoff.begin({'review_configuration': host.IDENTITY})
    with host.approval_lifecycle.run_scope(manifest, root.parents[1]):
        binding = original_execution(owner, manifest)
        monkeypatch.setenv(host.guard.EXECUTION_OWNERS_ENV, json.dumps([host.guard.process_identity(os.getpid())]))
        host.register(reviewers, owner=owner, source=parent, **binding)
        monkeypatch.delenv(host.guard.EXECUTION_OWNERS_ENV)
        line = native_call(parent, 'functions.exec', 'heartbeat ' + owner, 'next-heartbeat', {})
        host.heartbeat(owner=owner, source=parent, source_line=line, run_id=manifest['run_id'])
        shutdown.begin(owner=owner, reason='completed', run_id=manifest['run_id'])
        controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
        seal_execution(owner, manifest, run_id=manifest['run_id'])
        shutdown.finish(owner=owner, run_id=manifest['run_id'], **proofs(owner, child))
    registry_file = host.registry_file(owner, manifest['run_id'])
    registry = json.loads(registry_file.read_text())
    request_id = uuid4().hex
    request_file = host._request_file(action, 'finance', owner=owner, run_id=manifest['run_id'])
    request = {**original, 'request_id': request_id, 'run_id': manifest['run_id'],
               'controller_instance_id': manifest['controller_instance_id']}
    host.guard.write(request_file, request)
    registry['requests'] = {request_id: {'request_file': str(request_file),
                            'request_sha256': hashlib.sha256(request_file.read_bytes()).hexdigest()}}
    host.guard.write(registry_file, registry)
    evidence = {file: file.read_bytes() for file in tmp_path.rglob('*') if file.is_file()}
    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner,
                     '--run-id', first_run, '--run-id', manifest['run_id']]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['counts'] == {'completed': 1, 'excluded': 0, 'invalid': 0, 'duplicate': 1}
    assert result['selected_rounds'] == 2
    assert all(file.read_bytes() == raw for file, raw in evidence.items())


def test_cli_rejects_completion_artifact_symlink_even_when_bytes_match(exchange, tmp_path, capsys):
    root, owner, run, action, _, _ = completed_case(exchange, tmp_path)
    receipt = action.with_name('execution-receipt.json')
    original = tmp_path / 'copied-receipt.json'
    original.write_bytes(receipt.read_bytes())
    receipt.unlink()
    receipt.symlink_to(original)
    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner, '--run-id', run]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['counts']['completed'] == 0
    assert result['counts']['invalid'] == 1
