from __future__ import annotations

from apps.oa_finance_preaudit import attempt_state

# Select the retained supplier explicitly for recorded CLI/slot behavior.
from tests.finance_preaudit.review_profiles import retained_deepseek_cli  # noqa: F401

import hashlib
import json
from pathlib import Path
import subprocess
import shutil

import pytest

from apps.oa_finance_preaudit import codex_review_worker
from apps.oa_finance_preaudit.codex_review_worker import CodexExecution
from scripts import finance_preaudit_native_replay


@pytest.fixture
def recorded_case(tmp_path: Path):
    source = tmp_path / 'source'
    source.mkdir()
    subprocess.run(['git', 'init', '--quiet', str(source)], check=True)
    worker = source / 'apps/oa_finance_preaudit/codex_review_worker.py'
    worker.parent.mkdir(parents=True)
    shutil.copyfile(codex_review_worker.__file__, worker)
    rules = source / '.agents/skills/oa-finance-preaudit/references'
    rules.mkdir(parents=True)
    (rules / 'payment-screenshot-exemption.md').write_text('The current rule\n')
    (rules / 'confirmed-feedback.md').write_text('HISTORICAL ANSWER\n')
    (rules / 'regression-cases.json').write_text('{"expected": "pass"}')
    subprocess.run(['git', 'add', 'apps', '.agents'], cwd=source, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '--quiet', '-m', 'fixture source'], cwd=source, check=True)
    archive = tmp_path / 'archive' / 'attempt-1'
    archive.mkdir(parents=True)
    material = {'schema_version': 1, 'kind': 'preaudit_material', 'request_id': 'recorded-case',
                'workflow_key': 'general_expense', 'workflow_name': '通用费用报销',
                'declared_fields': [{'name': '报销金额', 'path': 'main.amount', 'value': '200'}],
                'authority_facts': [{'code': 'recorded_optional_fact', 'value': 'preserve me'}],
                'warnings': [], 'resources': [],
                'prepared': {'images': [], 'manifest': [], 'structured_contents': []}}
    material_file = archive / 'material.json'
    material_file.write_text(json.dumps(material))
    state_file = archive / 'state.json'
    state_file.write_text(json.dumps({'request_id': 'recorded-case', 'received_at': '2026-10-10 09:00:00',
        'workflow_key': 'general_expense',
        'material_file': str(tmp_path / 'old' / 'attempt-1' / 'material.json'),
        'material_fingerprint': 'sha256:' + hashlib.sha256(material_file.read_bytes()).hexdigest(),
        'evidence_fingerprint': 'sha256:' + hashlib.sha256(b'').hexdigest()}))
    inventory = tmp_path / 'inventory.json'
    inventory.write_text(json.dumps({'cases': [{'case': 'sample-a', 'material_file': str(material_file),
                                               'state_file': str(state_file)}]}))
    arguments = ['--prepare', '--case', 'sample-a', '--label', 'baseline',
        '--project', str(source), '--inventory', str(inventory), '--audit', str(tmp_path / 'audit')]
    return arguments, material_file, material


@pytest.fixture
def recorded_case_with_evidence(recorded_case):
    arguments, material_file, material = recorded_case
    archive = material_file.parent
    attachment = archive / 'attachments' / 'receipt.bin'
    page = archive / 'prepared' / 'receipt.png'
    attachment.parent.mkdir()
    page.parent.mkdir()
    attachment.write_bytes(b'synthetic original attachment')
    page.write_bytes(b'synthetic prepared page')
    state_file = archive / 'state.json'
    state = json.loads(state_file.read_text())
    original_attempt = Path(state['material_file']).parent
    material['resources'] = [{'request_id': 'recorded-case', 'resource_id': 'receipt-a',
        'source_resource_id': 'receipt-a',
        'local_readonly_path': str(original_attempt / 'attachments' / attachment.name)}]
    material['prepared']['images'] = [{'resource_id': 'receipt-a', 'kind': 'image',
        'path': str(original_attempt / 'prepared' / page.name)}]
    material['prepared']['manifest'] = [{'resource_id': 'receipt-a', 'status': 'prepared', 'image_count': 1}]
    material_file.write_text(json.dumps(material))
    state['material_fingerprint'] = 'sha256:' + hashlib.sha256(material_file.read_bytes()).hexdigest()
    evidence = b''.join(str(original_attempt / path.relative_to(archive)).encode() + b'\0'
                       + path.read_bytes() + b'\0' for path in (attachment, page))
    state['evidence_fingerprint'] = 'sha256:' + hashlib.sha256(evidence).hexdigest()
    state_file.write_text(json.dumps(state))
    return arguments, material_file, material


def test_prepare_freezes_complete_recorded_material_without_exposing_answers(recorded_case, capsys) -> None:
    arguments, material_file, material = recorded_case
    assert finance_preaudit_native_replay.main(arguments) == 0
    summary = json.loads(capsys.readouterr().out)
    action = attempt_state.read_action(Path(summary['action_file']))
    assert action['mode'] == 'preview'
    assert action['write_authorized'] is False
    state = json.loads(Path(action['state_file']).read_text())
    actual = json.loads(Path(state['material_file']).read_text())
    assert actual['declared_fields'] == material['declared_fields']
    assert actual['authority_facts'] == material['authority_facts']
    project = Path(summary['project_root'])
    assert (project / '.git').is_dir()
    assert not list(project.rglob('confirmed-feedback.md'))
    assert not list(project.rglob('regression-cases.json'))
    assert not (project / 'inventory.json').exists()
    assert not list(project.rglob('review-decision.json'))
    assert material_file.read_text() == json.dumps(material)


@pytest.mark.parametrize('staged', [False, True])
@pytest.mark.parametrize('relative', [
    '.agents/skills/oa-finance-preaudit/references/payment-screenshot-exemption.md',
    'apps/oa_finance_preaudit/codex_review_worker.py',
])
def test_prepare_rejects_uncommitted_snapshot_input(recorded_case, staged, relative) -> None:
    arguments, _, _ = recorded_case
    source = Path(arguments[arguments.index('--project') + 1])
    candidate = source / relative
    candidate.write_text('Uncommitted candidate input\n')
    if staged:
        subprocess.run(['git', 'add', str(candidate)], cwd=source, check=True)

    with pytest.raises(RuntimeError, match='source_snapshot_not_committed'):
        finance_preaudit_native_replay.main(arguments)

    audit = Path(arguments[arguments.index('--audit') + 1])
    assert not list(audit.rglob('prepare.json'))


def test_prepare_rejects_staged_rule_even_when_worktree_matches_head(recorded_case) -> None:
    arguments, _, _ = recorded_case
    source = Path(arguments[arguments.index('--project') + 1])
    rule = source / '.agents/skills/oa-finance-preaudit/references/payment-screenshot-exemption.md'
    rule.write_text('Staged candidate rule\n')
    subprocess.run(['git', 'add', str(rule)], cwd=source, check=True)
    rule.write_text('The current rule\n')

    with pytest.raises(RuntimeError, match='source_snapshot_not_committed'):
        finance_preaudit_native_replay.main(arguments)

    audit = Path(arguments[arguments.index('--audit') + 1])
    assert not list(audit.rglob('prepare.json'))


def test_prepare_rejects_changed_copy_even_when_source_is_restored(recorded_case, monkeypatch) -> None:
    arguments, _, _ = recorded_case
    source = Path(arguments[arguments.index('--project') + 1])
    rule = source / '.agents/skills/oa-finance-preaudit/references/payment-screenshot-exemption.md'
    copyfile = shutil.copyfile
    def changing_copyfile(src, target, *args, **kwargs):
        if Path(src) != rule:
            return copyfile(src, target, *args, **kwargs)
        rule.write_text('Transient uncommitted candidate\n')
        try:
            return copyfile(src, target, *args, **kwargs)
        finally:
            rule.write_text('The current rule\n')
    monkeypatch.setattr(shutil, 'copyfile', changing_copyfile)

    with pytest.raises(RuntimeError, match='source_snapshot_not_committed'):
        finance_preaudit_native_replay.main(arguments)

    assert rule.read_text() == 'The current rule\n'
    audit = Path(arguments[arguments.index('--audit') + 1])
    assert not list(audit.rglob('prepare.json'))


def test_prepare_ignores_changes_outside_snapshot_and_preserves_prior_label(recorded_case, capsys) -> None:
    arguments, _, _ = recorded_case
    source = Path(arguments[arguments.index('--project') + 1])
    references = source / '.agents/skills/oa-finance-preaudit/references'
    (references / 'confirmed-feedback.md').write_text('Changed historical answer\n')
    subprocess.run(['git', 'add', str(references / 'confirmed-feedback.md')], cwd=source, check=True)
    (source / 'local-notes.txt').write_text('Local notes outside the snapshot\n')
    (source / 'apps/untracked.py').write_text('UNTRACKED = True\n')
    assert finance_preaudit_native_replay.main(arguments) == 0
    prepared = json.loads(capsys.readouterr().out)
    project = Path(prepared['project_root'])
    manifest_path = Path(prepared['audit']) / 'prepare.json'
    first_manifest = json.loads(manifest_path.read_text())
    first_files = {path: path.read_bytes() for path in project.rglob('*') if path.is_file()}
    manifest_bytes = manifest_path.read_bytes()
    assert first_manifest['source_head'] == subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source).decode().strip()
    assert all((project / item['path']).read_bytes() == subprocess.check_output(
        ['git', 'show', first_manifest['source_head'] + ':' + item['path']], cwd=source)
        for item in first_manifest['source_manifest'])
    assert not (project / 'local-notes.txt').exists()
    assert not (project / 'apps/untracked.py').exists()
    assert not (project / references.relative_to(source) / 'confirmed-feedback.md').exists()

    rule = references / 'payment-screenshot-exemption.md'
    rule.write_text('The next committed rule\n')
    subprocess.run(['git', 'add', str(rule)], cwd=source, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '--quiet', '-m', 'next fixture rule'], cwd=source, check=True)
    next_arguments = ['next-version' if arg == 'baseline' else arg for arg in arguments]
    assert finance_preaudit_native_replay.main(next_arguments) == 0
    next_prepared = json.loads(capsys.readouterr().out)
    next_manifest = json.loads((Path(next_prepared['audit']) / 'prepare.json').read_text())
    assert next_manifest['source_head'] != first_manifest['source_head']
    assert (Path(next_prepared['project_root']) / rule.relative_to(source)).read_text() == 'The next committed rule\n'
    assert first_files == {path: path.read_bytes() for path in project.rglob('*') if path.is_file()}
    assert manifest_path.read_bytes() == manifest_bytes


def test_prepare_routes_frozen_travel_material_to_its_eight_review_items(recorded_case, capsys) -> None:
    arguments, material_file, material = recorded_case
    material['workflow_key'] = 'travel_expense'
    material['workflow_name'] = '差旅费报销'
    material_file.write_text(json.dumps(material))
    state_file = material_file.with_name('state.json')
    state = json.loads(state_file.read_text())
    state['workflow_key'] = 'travel_expense'
    state['material_fingerprint'] = 'sha256:' + hashlib.sha256(material_file.read_bytes()).hexdigest()
    state_file.write_text(json.dumps(state))

    assert finance_preaudit_native_replay.main(arguments) == 0
    summary = json.loads(capsys.readouterr().out)
    action = attempt_state.read_action(Path(summary['action_file']))
    packet = json.loads(Path(action['review_packet_file']).read_text())
    assert action['workflow_key'] == packet['workflow_key'] == 'travel_expense'
    assert [item['item_key'] for item in packet['items']] == [
        'reimbursement_period', 'travel_and_lodging_days', 'public_transport',
        'lodging_and_attendance', 'cross_region_vehicle', 'fuel_subsidy',
        'personal_advance_tickets', 'invoice_buyer',
    ]
    assert summary['mode'] == 'preview'
    assert summary['write_authorized'] is False


@pytest.mark.parametrize('state_location', ['archive', 'separate_directory', 'legacy_separate_directory'])
def test_run_uses_worker_without_source_checkout_and_preserves_recorded_pages(
        recorded_case_with_evidence, capsys, state_location) -> None:
    arguments, material_file, _material = recorded_case_with_evidence
    state_file = material_file.with_name('state.json')
    if state_location != 'archive':
        renamed_state = material_file.parent.parent / 'states' / 'recorded-state.json'
        renamed_state.parent.mkdir()
        state_file.rename(renamed_state)
        inventory_file = Path(arguments[arguments.index('--inventory') + 1])
        inventory = json.loads(inventory_file.read_text())
        inventory['cases'][0]['state_file'] = str(renamed_state)
        inventory_file.write_text(json.dumps(inventory))
        state_file = renamed_state
    archived_bytes = {path: path.read_bytes() for path in material_file.parent.rglob('*') if path.is_file()}
    original_state_bytes = state_file.read_bytes()
    finance_preaudit_native_replay.main(arguments)
    prepared = json.loads(capsys.readouterr().out)
    assert prepared['image_count'] == 1
    if state_location == 'legacy_separate_directory':
        manifest_file = Path(prepared['audit']) / 'prepare.json'
        manifest = json.loads(manifest_file.read_text())
        del manifest['original_state']
        manifest_file.write_text(json.dumps(manifest))
    shutil.rmtree(arguments[arguments.index('--project') + 1])
    calls = []
    def recorded_native(project, action_file, timeout):
        calls.append((project, action_file, timeout))
        action = attempt_state.read_action(action_file)
        packet = json.loads(Path(action['review_packet_file']).read_text())
        state = json.loads(Path(action['state_file']).read_text())
        material = json.loads(Path(state['material_file']).read_text())
        assert len(material['resources']) == len(material['prepared']['images']) == 1
        assert Path(material['resources'][0]['local_readonly_path']).read_bytes() == b'synthetic original attachment'
        assert Path(material['prepared']['images'][0]['path']).read_bytes() == b'synthetic prepared page'
        decision = {'schema_version': 1, 'kind': 'preaudit_review_decision',
                    'action_token': action['action_token'], 'workflow_key': 'general_expense',
                    'rule_version': packet['rule_version'], 'overall': 'needs_human',
                    'items': [{'item_key': item['item_key'], 'status': 'uncertain',
                               'conclusion': '付款金额尚未核实。', 'evidence_refs': [],
                               'missing_evidence': ['当前付款凭证不足']} for item in packet['items']]}
        return CodexExecution(None, {'status': 'unknown'}, decision, process_state='exited',
                              decision_state='valid_object', exit_code=0)
    run_arguments = ['--run' if value == '--prepare' else value for value in arguments]
    assert finance_preaudit_native_replay.main(run_arguments, executor=recorded_native) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert result['preview_closed_no_write'] is True
    assert result['write_state'] == 'not_requested'
    assert result['business_state'] == 'needs_human'
    assert result['execution_transport'] == 'recorded'
    assert result['business_acceptance'] == 'not_verified'
    assert result['original_material_unchanged'] is True
    decision = json.loads(Path(prepared['action_file']).with_name('review-decision.json').read_text())
    assert all(item['status'] == 'uncertain' for item in decision['items'])
    assert archived_bytes == {path: path.read_bytes() for path in material_file.parent.rglob('*') if path.is_file()}
    assert state_file.read_bytes() == original_state_bytes


def test_prepare_rejects_changed_original_evidence_before_freezing(recorded_case) -> None:
    arguments, material_file, material = recorded_case
    original_path = material_file.parent / 'attachments' / 'receipt.txt'
    original_path.parent.mkdir()
    original_path.write_bytes(b'original payment evidence')
    material['resources'] = [{'request_id': 'recorded-case', 'resource_id': 'receipt-a',
        'source_resource_id': 'receipt-a', 'local_readonly_path': str(original_path)}]
    material['prepared']['manifest'] = [{'resource_id': 'receipt-a', 'status': 'prepared', 'image_count': 0}]
    material_file.write_text(json.dumps(material))
    state_file = material_file.with_name('state.json')
    state = json.loads(state_file.read_text())
    state['material_file'] = str(material_file)
    state['material_fingerprint'] = 'sha256:' + hashlib.sha256(material_file.read_bytes()).hexdigest()
    state['evidence_fingerprint'] = 'sha256:' + hashlib.sha256(
        str(original_path).encode() + b'\0original payment evidence\0').hexdigest()
    state_file.write_text(json.dumps(state))
    original_path.write_bytes(b'changed payment evidence')
    with pytest.raises(ValueError, match='original_evidence_fingerprint_mismatch'):
        finance_preaudit_native_replay.main(arguments)


@pytest.mark.parametrize('dependency,mutation,error_code', [
    ('material.json', 'missing', 'original_material_unavailable'),
    ('material.json', 'unreadable', 'original_material_unavailable'),
    ('material.json', 'changed', 'original_material_changed'),
    ('state.json', 'missing', 'original_state_unavailable'),
    ('state.json', 'changed', 'original_state_changed'),
    ('attachments/receipt.bin', 'missing', 'original_evidence_unavailable'),
    ('attachments/receipt.bin', 'changed', 'original_evidence_changed'),
    ('prepared/receipt.png', 'missing', 'original_evidence_unavailable'),
    ('prepared/receipt.png', 'changed', 'original_evidence_changed'),
])
def test_run_rejects_unavailable_original_before_executing_review(
        recorded_case_with_evidence, capsys, monkeypatch, dependency, mutation, error_code):
    arguments, material_file, _ = recorded_case_with_evidence
    assert finance_preaudit_native_replay.main(arguments) == 0
    prepared = json.loads(capsys.readouterr().out)
    audit = Path(prepared['audit'])
    manifest_bytes = (audit / 'prepare.json').read_bytes()
    target = material_file.parent / dependency
    if mutation == 'missing':
        target.unlink()
    elif mutation == 'changed':
        target.write_bytes(b'changed original')
    else:
        read_bytes = Path.read_bytes
        def unreadable_original(path):
            if path == target:
                raise PermissionError('synthetic original read failure')
            return read_bytes(path)
        monkeypatch.setattr(Path, 'read_bytes', unreadable_original)

    assert finance_preaudit_native_replay.main(
        ['--run' if value == '--prepare' else value for value in arguments],
        executor=lambda *_args: pytest.fail('unavailable original started a review'),
    ) == 1

    result = json.loads(capsys.readouterr().out)
    assert result['error_code'] == error_code
    assert result['review_status'] == 'not_started'
    assert result['attempts'] == []
    assert result['business_acceptance'] == 'not_verified'
    assert result['oa_writes'] == 0
    assert json.loads((audit / 'result.json').read_text()) == result
    assert (audit / 'prepare.json').read_bytes() == manifest_bytes
    assert not (audit / 'worker-result.json').exists()
    assert not (audit / 'native-attempt-1').exists()
    assert not (audit / 'closure.json').exists()
    failure_bytes = (audit / 'result.json').read_bytes()
    with pytest.raises(RuntimeError, match='replay_result_already_exists_use_new_label'):
        finance_preaudit_native_replay.main(
            ['--run' if value == '--prepare' else value for value in arguments],
            executor=lambda *_args: pytest.fail('failed preflight was repeated'),
        )
    assert (audit / 'result.json').read_bytes() == failure_bytes


def test_run_keeps_executed_receipt_when_original_disappears(recorded_case, capsys):
    arguments, material_file, _ = recorded_case
    assert finance_preaudit_native_replay.main(arguments) == 0
    prepared = json.loads(capsys.readouterr().out)
    audit = Path(prepared['audit'])
    calls = []
    def recorded_native(_project, action_file, _timeout):
        calls.append(action_file)
        action = attempt_state.read_action(action_file)
        packet = json.loads(Path(action['review_packet_file']).read_text())
        material_file.unlink()
        return CodexExecution(None, {'status': 'unknown'}, {
            'schema_version': 1, 'kind': 'preaudit_review_decision',
            'action_token': action['action_token'], 'workflow_key': 'general_expense',
            'rule_version': packet['rule_version'], 'overall': 'needs_human',
            'items': [{'item_key': item['item_key'], 'status': 'uncertain',
                       'conclusion': '当前费用对应关系待核。', 'evidence_refs': [],
                       'missing_evidence': ['费用对应关系']} for item in packet['items']],
        }, process_state='exited', decision_state='valid_object', exit_code=0)
    run_arguments = ['--run' if value == '--prepare' else value for value in arguments]

    assert finance_preaudit_native_replay.main(run_arguments, executor=recorded_native) == 1

    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert result['review_status'] == 'reviewed'
    assert result['error_code'] == 'original_material_unavailable'
    assert result['original_material_unchanged'] is None
    assert len(result['attempts']) == 1
    assert result['attempts'][0]['exit_code'] == 0
    assert result['attempts'][0]['decision_state'] == 'valid_object'
    assert json.loads((audit / 'result.json').read_text()) == result
    assert json.loads((audit / 'worker-result.json').read_text())['status'] == 'reviewed'
    receipt = json.loads((audit / 'native-attempt-1' / 'receipt.json').read_text())
    assert receipt == result['attempts'][0]
    assert not (audit / 'closure.json').exists()
    with pytest.raises(RuntimeError, match='native_run_already_started_do_not_repeat'):
        finance_preaudit_native_replay.main(run_arguments,
            executor=lambda *_args: pytest.fail('an executed replay ran twice'))


def test_run_records_io_failure_from_started_execution_without_retry(recorded_case, capsys):
    arguments, material_file, _ = recorded_case
    original_bytes = material_file.read_bytes()
    assert finance_preaudit_native_replay.main(arguments) == 0
    audit = Path(json.loads(capsys.readouterr().out)['audit'])
    calls = []
    def unavailable_evidence(*_args):
        calls.append('started')
        raise PermissionError('synthetic evidence became unreadable')
    run_arguments = ['--run' if value == '--prepare' else value for value in arguments]

    assert finance_preaudit_native_replay.main(run_arguments, executor=unavailable_evidence) == 1

    result = json.loads(capsys.readouterr().out)
    assert calls == ['started']
    assert result['review_status'] == 'failed'
    assert result['error_code'] == 'native_replay_execution_io_error'
    assert len(result['attempts']) == 1
    receipt = json.loads((audit / 'native-attempt-1' / 'receipt.json').read_text())
    assert receipt == result['attempts'][0]
    assert receipt['error_code'] == 'native_replay_execution_io_error'
    assert receipt['io_error_type'] == 'PermissionError'
    assert receipt['process_state'] == 'unknown'
    assert receipt['exit_code'] is None
    assert result['business_acceptance'] == 'not_verified'
    assert result['oa_writes'] == 0
    assert json.loads((audit / 'result.json').read_text()) == result
    assert material_file.read_bytes() == original_bytes
    assert not (audit / 'worker-result.json').exists()
    assert not (audit / 'closure.json').exists()
    with pytest.raises(RuntimeError, match='native_run_already_started_do_not_repeat'):
        finance_preaudit_native_replay.main(run_arguments,
            executor=lambda *_args: pytest.fail('failed execution was retried'))


@pytest.fixture
def acceptance_records(recorded_case, capsys):
    """Offline artifact fixture; its native tag does not represent a model call."""
    arguments, _, _ = recorded_case
    finance_preaudit_native_replay.main(arguments)
    prepared = json.loads(capsys.readouterr().out)
    action_file = Path(prepared['action_file'])
    action = attempt_state.read_action(action_file)
    packet = json.loads(Path(action['review_packet_file']).read_text())
    def recorded_native(*_args):
        return CodexExecution(None, {'status': 'unknown'}, {
            'schema_version': 1, 'kind': 'preaudit_review_decision',
            'action_token': action['action_token'], 'workflow_key': 'general_expense',
            'rule_version': packet['rule_version'], 'overall': 'needs_human',
            'items': [{'item_key': item['item_key'], 'status': 'uncertain',
                       'conclusion': '具体费用对应关系待核。', 'evidence_refs': [],
                       'missing_evidence': ['具体费用对应关系']} for item in packet['items']],
        }, process_state='exited', decision_state='valid_object', exit_code=0)
    assert finance_preaudit_native_replay.main(
        ['--run' if x == '--prepare' else x for x in arguments], executor=recorded_native) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['business_acceptance'] == 'not_verified'
    audit = Path(prepared['audit'])
    result['execution_transport'] = 'native'
    finance_preaudit_native_replay.dump(audit / 'result.json', result)
    manifest = finance_preaudit_native_replay.read(audit / 'prepare.json')
    expectations = audit.parent.parent.parent / 'expectations.json'
    finance_preaudit_native_replay.dump(expectations, {
        'schema_version': 1, 'cases': [{'case': 'sample-a',
            'allowed_statuses': {i['item_key']: ['uncertain'] for i in packet['items']}}],
    })
    review = expectations.with_name('independent-review.json')
    finance_preaudit_native_replay.dump(review, {
        'schema_version': 1, 'case': 'sample-a', 'label': 'baseline',
        'source_head': manifest['source_head'],
        'expectations_sha256': finance_preaudit_native_replay.digest(expectations),
        'decision_sha256': result['decision_sha256'], 'verdict': 'accepted', 'findings': [],
    })
    initial_review_hash = finance_preaudit_native_replay.digest(review)
    def check(*, expected_hash=None, review_hash=None, output_file=None):
        check_args = ['--verify' if x == '--prepare' else x for x in arguments]
        if output_file is not None:
            check_args += ['--output-file', str(output_file)]
        return finance_preaudit_native_replay.main(check_args + [
            '--expectations', str(expectations), '--expectations-sha256', expected_hash or finance_preaudit_native_replay.digest(expectations),
            '--independent-review', str(review), '--review-sha256', review_hash or (finance_preaudit_native_replay.digest(review) if review.exists() else initial_review_hash),
        ])
    return check, audit, expectations, review


@pytest.mark.parametrize('finding', [
    'fuel_subsidy: page mileage-2 reads 45123; the opinion transcribed 45128',
    'supplementary_checks: omitted the observed storage-heading anomaly on page-1',
])
def test_verify_keeps_semantic_rejection_details_in_private_report(acceptance_records, capsys, finding):
    check, audit, _, review = acceptance_records
    independent = finance_preaudit_native_replay.read(review)
    independent.update(verdict='rejected', findings=[finding])
    finance_preaudit_native_replay.dump(review, independent)
    before = {p: p.read_bytes() for p in audit.rglob('*') if p.is_file()}
    target = review.with_name('acceptance-report.json')

    assert check(output_file=target) == 1

    output = capsys.readouterr().out
    summary = json.loads(output)
    report = finance_preaudit_native_replay.read(target)
    assert finding not in output
    assert summary['business_acceptance'] == 'not_accepted'
    assert summary['report_file']['path'] == str(target)
    assert summary['report_file']['sha256'] == finance_preaudit_native_replay.digest(target)
    assert report['independent_review']['findings'] == [finding]
    assert report['independent_review']['display_truncated'] is False
    assert not any('item_status_mismatch' in item for item in report['findings'])
    assert target.stat().st_mode & 0o777 == 0o600
    assert before == {p: p.read_bytes() for p in audit.rglob('*') if p.is_file()}


@pytest.mark.parametrize('destination', ['existing_report', 'review', 'material', 'symlink', 'parent_symlink', 'model_project', 'missing_parent'])
def test_verify_report_failure_preserves_evidence_and_rejects_acceptance(acceptance_records, capsys, destination):
    check, audit, _, review = acceptance_records
    manifest = finance_preaudit_native_replay.read(audit / 'prepare.json')
    target = review.with_name('acceptance-report.json')
    if destination == 'existing_report':
        target.write_text('previous report')
    elif destination == 'review':
        target = review
    elif destination == 'material':
        target = Path(manifest['original_material'])
    elif destination == 'symlink':
        target.symlink_to(review)
    elif destination == 'parent_symlink':
        parent = review.parent / 'linked-directory'
        parent.symlink_to(review.parent, target_is_directory=True)
        target = parent / target.name
    elif destination == 'model_project':
        target = Path(manifest['project_root']) / 'acceptance-report.json'
    elif destination == 'missing_parent':
        target = target.parent / 'missing' / target.name
    before = {p: p.read_bytes() for p in review.parent.parent.rglob('*') if p.is_file()}
    parent_mode = review.parent.stat().st_mode

    assert check(output_file=target) == 1

    summary = json.loads(capsys.readouterr().out)
    assert summary['business_acceptance'] == 'not_accepted'
    assert 'report_file' not in summary
    assert any('acceptance_report_file_failed' in finding for finding in summary['findings'])
    assert before == {p: p.read_bytes() for p in review.parent.parent.rglob('*') if p.is_file()}
    assert review.parent.stat().st_mode == parent_mode


@pytest.mark.parametrize('mistake', ['review_hash', 'expectations_hash', 'decision_binding', 'review_binding', 'invalid_findings'])
def test_verify_does_not_publish_unbound_semantic_details(acceptance_records, capsys, mistake):
    check, audit, _, review = acceptance_records
    finding = 'synthetic sensitive factual finding'
    independent = finance_preaudit_native_replay.read(review)
    independent.update(verdict='rejected', findings=[finding])
    if mistake == 'review_binding':
        independent['decision_sha256'] = '0' * 64
    if mistake == 'invalid_findings':
        independent['findings'] = {'untrusted': finding}
    finance_preaudit_native_replay.dump(review, independent)
    if mistake == 'decision_binding':
        result = finance_preaudit_native_replay.read(audit / 'result.json')
        result['decision_sha256'] = '0' * 64
        finance_preaudit_native_replay.dump(audit / 'result.json', result)
    options = {'review_hash': '0' * 64} if mistake == 'review_hash' else {}
    if mistake == 'expectations_hash':
        options['expected_hash'] = '0' * 64
    target = review.with_name('acceptance-report.json')

    assert check(output_file=target, **options) == 1

    output = capsys.readouterr().out
    assert finding not in output
    assert finding not in target.read_text()
    assert finance_preaudit_native_replay.read(target)['independent_review'] is None


def test_verify_bounds_semantic_report_without_changing_independent_review(acceptance_records, capsys):
    check, _, _, review = acceptance_records
    independent = finance_preaudit_native_replay.read(review)
    independent.update(verdict='rejected', findings=['x' * 1001, *[f'finding-{index}' for index in range(20)]])
    finance_preaudit_native_replay.dump(review, independent)
    before = review.read_bytes()
    target = review.with_name('acceptance-report.json')

    assert check(output_file=target) == 1

    summary = json.loads(capsys.readouterr().out)
    details = finance_preaudit_native_replay.read(target)['independent_review']
    assert summary['independent_review_findings_count'] == 21
    assert details['total'] == 21
    assert len(details['findings']) == 20
    assert details['findings'][0] == 'x' * 1000
    assert details['display_truncated'] is True
    assert review.read_bytes() == before


def test_verify_does_not_issue_report_receipt_after_output_io_failure(acceptance_records, capsys, monkeypatch):
    check, _, _, review = acceptance_records
    finding = 'synthetic sensitive review detail'
    independent = finance_preaudit_native_replay.read(review)
    independent.update(verdict='rejected', findings=[finding])
    finance_preaudit_native_replay.dump(review, independent)
    before = review.read_bytes()
    target = review.with_name('acceptance-report.json')
    def failed_flush(_descriptor):
        raise OSError('synthetic flush failure')
    monkeypatch.setattr(finance_preaudit_native_replay.os, 'fsync', failed_flush)

    assert check(output_file=target) == 1

    output = capsys.readouterr().out
    summary = json.loads(output)
    assert finding not in output
    assert 'report_file' not in summary
    assert any('acceptance_report_file_failed' in item for item in summary['findings'])
    assert target.stat().st_mode & 0o777 == 0o600
    assert review.read_bytes() == before


def test_verify_requires_bound_independent_business_review(acceptance_records, capsys):
    check, audit, _, _ = acceptance_records
    before = {p: p.read_bytes() for p in audit.rglob('*') if p.is_file()}
    assert check() == 0
    report = json.loads(capsys.readouterr().out)
    assert report['business_acceptance'] == 'accepted'
    assert report['source_head'] == finance_preaudit_native_replay.read(audit / 'prepare.json')['source_head']
    assert report['qualification_scope'] == 'single_frozen_native_case'
    assert report['findings'] == []
    assert before == {p: p.read_bytes() for p in audit.rglob('*') if p.is_file()}


@pytest.mark.parametrize('mistake,code', [
    ('wrong_status', 'item_status_mismatch'), ('semantic_rejected', 'independent_review_not_accepted'),
    ('review_for_other_decision', 'independent_review_binding_mismatch'),
    ('recorded_transport', 'native_model_evidence_unverified'), ('missing_review', 'acceptance_evidence_unreadable'),
    ('changed_rule', 'frozen_source_changed'), ('changed_material', 'original_material_changed'),
    ('changed_decision', 'decision_fingerprint_mismatch'),
    ('changed_fresh_material', 'frozen_review_inputs_changed'),
    ('deleted_fresh_material', 'frozen_review_inputs_changed'),
    ('changed_packet', 'frozen_review_inputs_changed'),
    ('deleted_packet', 'frozen_review_inputs_changed'),
])
def test_verify_rejects_unqualified_results(acceptance_records, capsys, mistake, code):
    check, audit, expectations, review = acceptance_records
    manifest = finance_preaudit_native_replay.read(audit / 'prepare.json')
    if mistake == 'wrong_status':
        data = finance_preaudit_native_replay.read(expectations)
        for key in data['cases'][0]['allowed_statuses']:
            data['cases'][0]['allowed_statuses'][key] = ['pass']
        finance_preaudit_native_replay.dump(expectations, data)
        data = finance_preaudit_native_replay.read(review)
        data['expectations_sha256'] = finance_preaudit_native_replay.digest(expectations)
        finance_preaudit_native_replay.dump(review, data)
    elif mistake.startswith('semantic') or mistake == 'review_for_other_decision':
        data = finance_preaudit_native_replay.read(review)
        data['verdict' if mistake.startswith('semantic') else 'decision_sha256'] = 'rejected' if mistake.startswith('semantic') else '0' * 64
        finance_preaudit_native_replay.dump(review, data)
    elif mistake == 'recorded_transport':
        data = finance_preaudit_native_replay.read(audit / 'result.json')
        data['execution_transport'] = 'recorded'
        finance_preaudit_native_replay.dump(audit / 'result.json', data)
    elif mistake == 'missing_review':
        # Keep the caller's frozen digest while the corresponding file disappears.
        review.unlink()
    elif mistake == 'changed_rule':
        p = Path(manifest['project_root']) / manifest['source_manifest'][0]['path']
        p.write_text(p.read_text() + '\nchanged rule\n')
    elif mistake == 'changed_material':
        Path(manifest['original_material']).write_text('{}')
    elif mistake in {'changed_fresh_material', 'deleted_fresh_material', 'changed_packet', 'deleted_packet'}:
        action = attempt_state.read_action(Path(manifest['action_file']))
        state = finance_preaudit_native_replay.read(action['state_file'])
        p = Path(state['material_file'] if mistake.endswith('material') else action['review_packet_file'])
        if mistake.startswith('deleted'):
            p.unlink()
        else:
            data = finance_preaudit_native_replay.read(p)
            if mistake.endswith('material'):
                data['declared_fields'][0]['value'] = '999999'
            else:
                data['rule_version'] = 'unreviewed-tampered-rule'
            finance_preaudit_native_replay.dump(p, data)
    elif mistake == 'changed_decision':
        Path(manifest['action_file']).with_name('native-preview-decision.json').write_text('{}')
    assert check() == 1
    report = json.loads(capsys.readouterr().out)
    assert report['business_acceptance'] == 'not_accepted'
    assert any(code in finding for finding in report['findings'])


@pytest.mark.parametrize('hash_argument,code', [
    ('expected_hash', 'expectations_fingerprint_mismatch'), ('review_hash', 'review_fingerprint_mismatch'),
])
def test_verify_rejects_mismatching_independently_frozen_hash(acceptance_records, capsys, hash_argument, code):
    check, _, _, _ = acceptance_records
    assert check(**{hash_argument: '0' * 64}) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['business_acceptance'] == 'not_accepted'
    assert any(code in finding for finding in report['findings'])


@pytest.mark.parametrize('provider', ['deepseek', 'openai'])
def test_prepared_provider_is_frozen_and_cannot_change_at_run(recorded_case, capsys, monkeypatch, provider):
    from apps.oa_finance_preaudit import production_config, codex_review_worker
    monkeypatch.setenv(production_config.REVIEW_PROVIDER_ENV, provider)
    monkeypatch.setenv(production_config.REVIEW_TRANSPORT_ENV, "cli" if provider == "deepseek" else "host_chat")
    monkeypatch.setattr(codex_review_worker, 'REVIEW_SETTINGS', production_config.review_settings(provider))
    arguments, _, _ = recorded_case
    assert finance_preaudit_native_replay.main(arguments) == 0
    summary = json.loads(capsys.readouterr().out)
    manifest_file = Path(summary["audit"]) / "prepare.json"
    manifest = json.loads(manifest_file.read_text())
    assert manifest["review_configuration"] == production_config.review_settings(provider)
    other = 'openai' if provider == 'deepseek' else 'deepseek'
    manifest["review_configuration"] = production_config.review_settings(other, transport="cli" if other == "deepseek" else "host_chat")
    manifest_file.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="review_model_identity_changed"):
        finance_preaudit_native_replay.main(["--run" if arg == "--prepare" else arg for arg in arguments],
                                           executor=lambda *args: pytest.fail("changed provider started a model"))
    assert not manifest_file.with_name("worker-result.json").exists()


def test_default_app_replay_refuses_run_before_consuming_prepared_preview(recorded_case, capsys, monkeypatch):
    from apps.oa_finance_preaudit import production_config
    monkeypatch.delenv(production_config.REVIEW_PROVIDER_ENV, raising=False)
    monkeypatch.delenv(production_config.REVIEW_TRANSPORT_ENV, raising=False)
    monkeypatch.setattr(codex_review_worker, 'REVIEW_SETTINGS', production_config.review_settings())
    arguments, original_material, _ = recorded_case
    original_bytes = original_material.read_bytes()
    assert finance_preaudit_native_replay.main(arguments) == 0
    prepared = json.loads(capsys.readouterr().out)
    audit = Path(prepared['audit'])
    prepared_bytes = (audit / 'prepare.json').read_bytes()
    action_file = Path(prepared['action_file'])
    action_bytes = action_file.read_bytes()
    state_file = action_file.with_name('state.json')
    state_bytes = state_file.read_bytes()
    run_arguments = ['--run' if arg == '--prepare' else arg for arg in arguments]
    for _ in range(2):
        with pytest.raises(ValueError, match='native_replay_transport_unsupported.*OA_REVIEW_PROVIDER=deepseek'):
            finance_preaudit_native_replay.main(run_arguments,
                executor=lambda *_args: pytest.fail('unsupported App replay invoked an executor'))
    assert sorted(path.name for path in audit.iterdir()) == ['prepare.json']
    assert (audit / 'prepare.json').read_bytes() == prepared_bytes
    assert action_file.read_bytes() == action_bytes
    assert state_file.read_bytes() == state_bytes
    assert original_material.read_bytes() == original_bytes
