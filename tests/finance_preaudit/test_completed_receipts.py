from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from apps.oa_finance_preaudit import cli, controller_handoff
from apps.oa_finance_preaudit import app_review_shutdown as shutdown
from tests.finance_preaudit.test_app_review_shutdown import proofs
from tests.finance_preaudit.test_host_chat_review import exchange, seal_execution


def test_cli_completed_receipts_does_not_load_current_model_settings(tmp_path):
    code = '''
import sys
from apps.oa_finance_preaudit import production_config
def unavailable():
    raise RuntimeError("current_model_settings_must_not_be_loaded")
production_config.review_settings = unavailable
from apps.oa_finance_preaudit import cli
raise SystemExit(cli.main(['completed-receipts', '--root', sys.argv[1],
    '--owner-task-id', '00000000-0000-0000-0000-000000000001', '--run-id', '0' * 32]))
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[2], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['counts'] == {'completed': 0, 'excluded': 0, 'invalid': 1, 'duplicate': 0}
    assert report['gaps'] == {'sealed_round_unverified': 1}


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


def completed_case(exchange, tmp_path, *, controlled=True, legacy=False, close_action=True, submitted=True,
                   workflow='business_support_fee', bootstrap_revision=None, bootstrap_dirty=False,
                   bootstrap_before_start=False, bootstrap_native_mismatch=False, close_before_shutdown=False):
    from apps import json_artifact
    from apps.oa_finance_preaudit import host_chat_review as host
    from tests.finance_preaudit.test_flow_engine_support import _engine, _decision, _reviewed_items
    from tests.finance_preaudit.test_host_chat_review import draft_source, native_call
    action, _, owner, child, _, manifest, _ = exchange
    if bootstrap_revision is not None:
        manifest.update(code_revision=bootstrap_revision, code_dirty=bootstrap_dirty)
        host.guard.write(controller_handoff.manifest_file().parent / 'executions' /
                         manifest['invocation_id'] / 'controller-manifest.json', manifest)
    root = action.parents[4]
    engine = _engine(root, material_workflow=workflow, controlled=controlled)
    opened = engine.next_action(workflow, mode='controlled_write' if controlled else 'preview')
    current = Path(opened['action_file'])
    if legacy:
        state_file = current.with_name('state.json')
        state = json.loads(state_file.read_text())
        state.pop('action_reference_sha256')
        json_artifact.write_json_atomic(current, opened)
        json_artifact.write_json_atomic(state_file, state)
    host.approval_lifecycle.heartbeat(manifest['run_id'], manifest['controller_instance_id'], {current}, [], writer_active=False)
    request_file = host.publish(current, prompt='Read the frozen support materials.', timeout_seconds=60)
    if bootstrap_revision is not None:
        from datetime import datetime
        import hashlib
        registered_file = host.registry_file(owner, manifest['run_id'])
        registered = json.loads(registered_file.read_text())
        request = json.loads(request_file.read_text())
        started = datetime.fromisoformat(manifest['started_at']).timestamp()
        request['created_at'] = started - 1 if bootstrap_before_start else (started + registered['registered_at']) / 2
        host.guard.write(request_file, request)
        registered['requests'][request['request_id']]['request_sha256'] = hashlib.sha256(request_file.read_bytes()).hexdigest()
        host.guard.write(registered_file, registered)
    parent = child.with_name('parent.jsonl')
    dispatch = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request_file)},
                           'completion-dispatch', {})
    host.assign(request_file, slot=0, owner=owner, source=parent, source_line=dispatch)
    decision = tmp_path / 'completed-decision.json'
    if workflow == 'business_support_fee':
        payload = _decision(opened, _reviewed_items(['pass'] * 4))
        for field in ('schema_version', 'kind', 'action_token', 'workflow_key', 'rule_version'):
            payload.pop(field)
    else:
        packet = json.loads(current.with_name('review-packet.json').read_text())
        payload = {'overall': 'needs_human', 'items': [
            {'item_key': item['item_key'], 'status': 'uncertain', 'conclusion': '需核对原始材料。',
             'evidence_refs': [], 'missing_evidence': ['缺少可核实材料']} for item in packet['items']]}
        if 'company_comparison_contract' in packet:
            payload['company_comparison'] = {'observed_refs': [], 'result': 'unknown', 'reason': '需核对公司信息。'}
    line = draft_source(child, request_file, decision, owner, payload=payload)
    if submitted:
        host.submit(request_file, decision, owner=owner, source=child, source_line=line)
    if close_action:
        consumed = tmp_path / 'consumed-decision.json'
        json_artifact.write_json_atomic(consumed, host.read_result(request_file)['decision'])
        engine.complete_review(current, consumed)
    if not close_before_shutdown:
        shutdown.begin(owner=owner, reason='completed' if close_action else 'cancelled')
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    execution = seal_execution(owner, manifest)
    if close_before_shutdown:
        idle = native_call(parent, 'list_agents', {}, 'independent-close-idle', {'agents': [
            {'agent_name': f'/root/reviewer_{slot}', 'agent_status': 'completed'} for slot in range(3)]})
        host.close(owner=owner, reason='completed', source=parent, source_line=idle, run_id=manifest['run_id'])
        shutdown.begin(owner=owner, reason='completed', run_id=manifest['run_id'])
    shutdown_proofs = proofs(owner, child)
    if bootstrap_revision is not None:
        source = shutdown_proofs['source']
        rows = source.read_text().splitlines()
        record = json.loads(rows[shutdown_proofs['wait_line']])
        recorded_execution = json.loads(execution.read_text())
        recorded_execution['receipt_file'] = str(execution)
        if bootstrap_native_mismatch:
            recorded_execution['code_revision'] = '3a424978234dcf85b9176256925a8ac609e4c322'
        record['payload']['output'] = json.dumps({'exit_code': 0, 'output': json.dumps(recorded_execution)})
        rows[shutdown_proofs['wait_line']] = json.dumps(record)
        source.write_text('\n'.join(rows) + '\n')
    shutdown.finish(owner=owner, **shutdown_proofs)
    return root, owner, manifest['run_id'], current, host.registry_file(owner, manifest['run_id']), request_file


def test_cli_counts_completed_exchange_sealed_after_its_original_close(exchange, tmp_path, capsys):
    root, owner, run, _, registry_file, _ = completed_case(exchange, tmp_path, close_before_shutdown=True)
    registered = json.loads(registry_file.read_text())
    assert registered['closed_at'] < registered['shutdown']['started_at']
    assert registered['shutdown']['status'] == 'verified'
    original = {file: file.read_bytes() for file in tmp_path.rglob('*') if file.is_file()}

    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner, '--run-id', run]) == 0

    assert json.loads(capsys.readouterr().out)['counts'] == {
        'completed': 1, 'excluded': 0, 'invalid': 0, 'duplicate': 0}
    assert all(file.read_bytes() == raw for file, raw in original.items())


@pytest.mark.parametrize('damage', ['missing_idle', 'idle_source', 'idle_running', 'completion_after_close',
                                  'verified_before_shutdown', 'verified_before_last_tool', 'after_closed', 'after_shutdown'])
def test_cli_refuses_unproven_late_completed_shutdown(exchange, tmp_path, capsys, damage):
    from datetime import datetime
    import hashlib
    from apps.oa_finance_preaudit import host_chat_review as host
    root, owner, run, _, registry_file, request_file = completed_case(exchange, tmp_path, close_before_shutdown=True)
    registered = json.loads(registry_file.read_text())
    request = json.loads(request_file.read_text())
    assigned = registered['assignments'][request['request_id']]
    plan = registered['shutdown']
    if damage == 'missing_idle':
        registered.pop('children_idle_source')
    elif damage == 'idle_source':
        registered['children_idle_source']['session_id'] = 'unrelated-owner'
    elif damage == 'idle_running':
        saved = registered['children_idle_source']
        path = Path(saved['session_file'])
        rows = path.read_text().splitlines()
        record = json.loads(rows[saved['output_line'] - 1])
        output = json.loads(record['payload']['output'])
        output['agents'][0]['agent_status'] = 'running'
        record['payload']['output'] = json.dumps(output)
        rows[saved['output_line'] - 1] = json.dumps(record)
        path.write_text('\n'.join(rows) + '\n')
    elif damage == 'completion_after_close':
        path = Path(registered['original_execution']['receipt_file'])
        execution = json.loads(path.read_text())
        execution['completed_at'] = datetime.fromtimestamp(
            (registered['closed_at'] + plan['started_at']) / 2, tz=datetime.now().astimezone().tzinfo).isoformat()
        host.guard.write(path, execution)
        plan['receipt']['execution_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif damage == 'verified_before_shutdown':
        plan['verified_at'] = plan['started_at'] - .001
    elif damage == 'verified_before_last_tool':
        saved = plan['receipt']['driver_wait_source']
        row = json.loads(Path(saved['session_file']).read_text().splitlines()[saved['line'] - 1])
        plan['verified_at'] = datetime.fromisoformat(row['timestamp']).timestamp() - .000001
    elif damage == 'after_closed':
        registered['closed_at'] = assigned['submitted_at'] - .001
    else:
        plan['started_at'] = assigned['submitted_at'] - .001
    host.guard.write(registry_file, registered)
    original = {file: file.read_bytes() for file in tmp_path.rglob('*') if file.is_file()}

    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner, '--run-id', run]) == 0

    assert json.loads(capsys.readouterr().out)['counts'] == {
        'completed': 0, 'excluded': 0, 'invalid': 1, 'duplicate': 0}
    assert all(file.read_bytes() == raw for file, raw in original.items())


def test_cli_counts_a_sealed_legacy_bootstrap_request_created_before_registration(exchange, tmp_path, capsys):
    root, owner, run, _, _, _ = completed_case(
        exchange, tmp_path, bootstrap_revision='64a85b569b94a32ff7b7c0efebff10be05c07cd4')
    original = {file: file.read_bytes() for file in tmp_path.rglob('*') if file.is_file()}

    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner, '--run-id', run]) == 0

    assert json.loads(capsys.readouterr().out)['counts'] == {
        'completed': 1, 'excluded': 0, 'invalid': 0, 'duplicate': 0}
    assert all(file.read_bytes() == raw for file, raw in original.items())


@pytest.mark.parametrize('damage', ['migration_revision', 'current_revision', 'missing_git_object', 'dirty',
                                  'before_run', 'dispatch_before_registration', 'closed_before_shutdown',
                                  'native_revision_mismatch', 'missing_revision'])
def test_cli_rejects_untrusted_or_new_format_bootstrap_history(exchange, tmp_path, capsys, damage):
    from apps.oa_finance_preaudit import host_chat_review as host
    revision = {'migration_revision': '40cbb88a163335a7bf8f3d3e985f39ed7937f76c',
                'current_revision': '3a424978234dcf85b9176256925a8ac609e4c322',
                'missing_git_object': 'f' * 40}.get(damage, '64a85b569b94a32ff7b7c0efebff10be05c07cd4')
    root, owner, run, _, registry_file, request_file = completed_case(
        exchange, tmp_path, bootstrap_revision=revision, bootstrap_dirty=damage == 'dirty',
        bootstrap_before_start=damage == 'before_run', bootstrap_native_mismatch=damage == 'native_revision_mismatch')
    registered = json.loads(registry_file.read_text())
    original_manifest = None
    if damage == 'dispatch_before_registration':
        request = json.loads(request_file.read_text())
        registered['registered_at'] = registered['assignments'][request['request_id']]['dispatched_at'] + .001
    elif damage == 'closed_before_shutdown':
        registered['closed_at'] = registered['shutdown']['started_at'] - .001
    elif damage == 'missing_revision':
        path = Path(registered['original_execution']['receipt_file']).with_name('controller-manifest.json')
        original_manifest = path, path.read_bytes()
        manifest = json.loads(path.read_text())
        manifest.pop('code_revision')
        host.guard.write(path, manifest)
    host.guard.write(registry_file, registered)
    original = {file: file.read_bytes() for file in tmp_path.rglob('*') if file.is_file()}

    try:
        assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner, '--run-id', run]) == 0

        assert json.loads(capsys.readouterr().out)['counts'] == {
            'completed': 0, 'excluded': 0, 'invalid': 1, 'duplicate': 0}
        assert all(file.read_bytes() == raw for file, raw in original.items())
    finally:
        if original_manifest is not None:
            original_manifest[0].write_bytes(original_manifest[1])


def test_cli_never_counts_a_request_without_native_assignment(exchange, tmp_path, capsys):
    from apps.oa_finance_preaudit import host_chat_review as host
    root, owner, run, _, registry_file, _ = completed_case(exchange, tmp_path)
    registry = json.loads(registry_file.read_text())
    registry['assignments'] = {}
    host.guard.write(registry_file, registry)
    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner, '--run-id', run]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['counts']['completed'] == 0
    assert result['counts']['invalid'] == 1


@pytest.mark.parametrize('damage', ['dispatch', 'draft_source', 'draft_bytes', 'missing_submission',
                                  'submission_decision', 'late_submission', 'reviewer', 'dispatch_reused'])
def test_cli_revalidates_each_rounds_native_submission(exchange, tmp_path, capsys, damage):
    import hashlib
    from apps.oa_finance_preaudit import host_chat_review as host
    root, owner, run, _, registry_file, request_file = completed_case(exchange, tmp_path)
    registry = json.loads(registry_file.read_text())
    request = json.loads(request_file.read_text())
    assigned = registry['assignments'][request['request_id']]
    submission_file = host._submission_file(request_file)
    submitted = json.loads(submission_file.read_text())
    if damage in {'dispatch', 'draft_source'}:
        proof = assigned['dispatch_source'] if damage == 'dispatch' else submitted['source']
        source = Path(proof['session_file'])
        rows = source.read_text().splitlines()
        index = proof['line'] - 1
        row = json.loads(rows[index])
        if damage == 'dispatch':
            row['payload']['arguments'] = json.dumps({'target': '/root/reviewer_0', 'message': 'unrelated request'})
        else:
            row['payload']['input'] += '\ntext("unrelated statement");'
        rows[index] = json.dumps(row)
        source.write_text('\n'.join(rows) + '\n')
    elif damage == 'draft_bytes':
        Path(submitted['source']['decision_file']).write_text('{}')
    elif damage == 'missing_submission':
        submission_file.unlink()
    elif damage == 'dispatch_reused':
        registry['assignments']['unrelated-request'] = dict(assigned)
        host.guard.write(registry_file, registry)
    else:
        if damage == 'submission_decision':
            submitted['decision']['items'][0]['conclusion'] = 'A different decision with the same status.'
        elif damage == 'late_submission':
            submitted['submitted_at'] = assigned['submitted_at'] = registry['shutdown']['started_at'] + .001
        else:
            assigned['reviewer_task_id'] = registry['reviewers'][1]['reviewer_task_id']
            submitted['assignment']['reviewer_task_id'] = assigned['reviewer_task_id']
        host.guard.write(submission_file, submitted)
        assigned['submission_sha256'] = hashlib.sha256(submission_file.read_bytes()).hexdigest()
        host.guard.write(registry_file, registry)
    original = {file: file.read_bytes() for file in tmp_path.rglob('*') if file.is_file()}

    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner, '--run-id', run]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result['counts']['completed'] == 0
    assert result['counts']['invalid'] == 1
    assert all(file.read_bytes() == raw for file, raw in original.items())


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


@pytest.mark.parametrize('legacy,state_version,workflow,changed', [
    (True, None, 'small_marketing_fee', None), (True, 'small_marketing_fee:1', 'small_marketing_fee', None),
    (False, None, 'small_marketing_fee', None), (True, None, 'tpm_small_marketing_fee', None),
    (True, None, 'small_marketing_fee', 'review-packet.json'),
    (True, None, 'small_marketing_fee', 'review-decision.json'),
])
def test_cli_reconstructs_missing_legacy_version_from_bound_completion(
    exchange, tmp_path, capsys, legacy, state_version, workflow, changed,
):
    from apps.oa_finance_preaudit import host_chat_review as host
    root, owner, run, action, _, _ = completed_case(
        exchange, tmp_path, legacy=legacy, workflow=workflow)
    state_file = action.with_name('state.json')
    state = json.loads(state_file.read_text())
    if state_version is None:
        state.pop('rule_version')
    else:
        state['rule_version'] = state_version
    host.guard.write(state_file, state)
    if changed is not None:
        path = action.with_name(changed)
        payload = json.loads(path.read_text())
        payload['rule_version'] = 'small_marketing_fee:conflicting'
        host.guard.write(path, payload)
    original = {file: file.read_bytes() for file in tmp_path.rglob('*') if file.is_file()}
    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner, '--run-id', run]) == 0
    accepted = legacy and state_version is None and changed is None
    assert json.loads(capsys.readouterr().out)['counts'] == {
        'completed': int(accepted), 'excluded': 0, 'invalid': int(not accepted), 'duplicate': 0}
    assert all(file.read_bytes() == raw for file, raw in original.items())


@pytest.mark.parametrize('old_submitted,renewed', [(True, False), (False, False), (True, True)])
def test_cli_deduplicates_same_action_across_independently_sealed_rounds(
    exchange, tmp_path, monkeypatch, capsys, old_submitted, renewed,
):
    import os
    from uuid import uuid4
    from apps.oa_finance_preaudit import host_chat_review as host
    from tests.finance_preaudit.test_app_reviewer_slots import original_execution
    from tests.finance_preaudit.test_host_chat_review import native_call
    from apps import json_artifact
    from apps.oa_approval import runtime_control
    from tests.finance_preaudit.test_flow_engine_support import _engine, _grant
    from apps.oa_finance_preaudit import flow_engine
    from tests.finance_preaudit.test_host_chat_review import draft_source
    root, owner, first_run, action, _, original_request = completed_case(exchange, tmp_path, close_action=False, submitted=old_submitted)
    original = json.loads((tmp_path / 'completed-decision.json').read_text())
    original_request_bytes = original_request.read_bytes()
    original_draft_bytes = (tmp_path / 'completed-decision.json').read_bytes()
    assert runtime_control.clear_cycle_stop()
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
        host.approval_lifecycle.heartbeat(manifest['run_id'], manifest['controller_instance_id'], {action}, [], writer_active=False)
        if renewed:
            engine = flow_engine.FlowEngine(root=root, discover_candidates=lambda _: (),
                collect_material=lambda *_: pytest.fail('renewal_collected_new_material'),
                authorization_source=_grant,
                authorization_verifier=lambda grant, context: grant == _grant(context),
                authorization_renewer=lambda _previous, context: _grant(context),
                authorization_refresh_needed=lambda _: True,
                submit_frozen=lambda *_: pytest.fail('renewal_submitted'))
            engine.next_action('business_support_fee', mode='controlled_write')
        request_file = host.publish(action, prompt='Review the still-open frozen action.', timeout_seconds=60)
        dispatch = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request_file)},
                               'next-completion-dispatch', {})
        host.assign(request_file, slot=0, owner=owner, source=parent, source_line=dispatch)
        target = tmp_path / 'next-completed-decision.json'
        source_line = draft_source(Path(reviewers[0]['session_file']), request_file, target, owner, payload=original)
        host.submit(request_file, target, owner=owner, source=Path(reviewers[0]['session_file']), source_line=source_line)
        consumed = tmp_path / 'next-consumed-decision.json'
        json_artifact.write_json_atomic(consumed, host.read_result(request_file)['decision'])
        _engine(root, controlled=True).complete_review(action, consumed)
        shutdown.begin(owner=owner, reason='completed', run_id=manifest['run_id'])
        controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
        seal_execution(owner, manifest, run_id=manifest['run_id'])
        shutdown.finish(owner=owner, run_id=manifest['run_id'], **proofs(owner, child))
    evidence = {file: file.read_bytes() for file in tmp_path.rglob('*') if file.is_file()}
    assert cli.main(['completed-receipts', '--root', str(root), '--owner-task-id', owner,
                     '--run-id', first_run, '--run-id', manifest['run_id']]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['counts'] == {'completed': 1, 'excluded': int(not old_submitted or renewed),
                                'invalid': 0, 'duplicate': int(old_submitted and not renewed)}
    assert original_request.read_bytes() == original_request_bytes
    assert (tmp_path / 'completed-decision.json').read_bytes() == original_draft_bytes
    assert result['selected_rounds'] == 2
    assert all(file.read_bytes() == raw for file, raw in evidence.items())
    if renewed:
        selected = ['completed-receipts', '--root', str(root), '--owner-task-id', owner]
        assert cli.main([*selected, '--run-id', manifest['run_id'], '--run-id', first_run]) == 0
        assert json.loads(capsys.readouterr().out)['counts'] == result['counts']
        assert cli.main([*selected, '--run-id', first_run]) == 0
        assert json.loads(capsys.readouterr().out)['counts'] == {
            'completed': 0, 'excluded': 0, 'invalid': 1, 'duplicate': 0}
        old_submission = json.loads(host._submission_file(original_request).read_text())
        source = Path(old_submission['source']['session_file'])
        rows = source.read_text().splitlines()
        index = old_submission['source']['line'] - 1
        row = json.loads(rows[index])
        row['payload']['input'] += '\ntext("unrelated statement");'
        rows[index] = json.dumps(row)
        source.write_text('\n'.join(rows) + '\n')
        evidence = {file: file.read_bytes() for file in tmp_path.rglob('*') if file.is_file()}
        assert cli.main([*selected, '--run-id', first_run, '--run-id', manifest['run_id']]) == 0
        assert json.loads(capsys.readouterr().out)['counts'] == {
            'completed': 1, 'excluded': 0, 'invalid': 1, 'duplicate': 0}
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
