from __future__ import annotations

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


def test_prepare_freezes_complete_recorded_material_without_exposing_answers(recorded_case, capsys) -> None:
    arguments, material_file, material = recorded_case
    assert finance_preaudit_native_replay.main(arguments) == 0
    summary = json.loads(capsys.readouterr().out)
    action = json.loads(Path(summary['action_file']).read_text())
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
    action = json.loads(Path(summary['action_file']).read_text())
    packet = json.loads(Path(action['review_packet_file']).read_text())
    assert action['workflow_key'] == packet['workflow_key'] == 'travel_expense'
    assert [item['item_key'] for item in packet['items']] == [
        'reimbursement_period', 'travel_and_lodging_days', 'public_transport',
        'lodging_and_attendance', 'cross_region_vehicle', 'fuel_subsidy',
        'personal_advance_tickets', 'invoice_buyer',
    ]
    assert summary['mode'] == 'preview'
    assert summary['write_authorized'] is False


def test_run_uses_worker_then_closes_preview_with_real_anomalies_preserved(recorded_case, capsys) -> None:
    arguments, _material_file, _material = recorded_case
    finance_preaudit_native_replay.main(arguments)
    prepared = json.loads(capsys.readouterr().out)
    calls = []
    def recorded_native(project, action_file, timeout):
        calls.append((project, action_file, timeout))
        action = json.loads(action_file.read_text())
        packet = json.loads(Path(action['review_packet_file']).read_text())
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
    assert result['original_material_unchanged'] is True
    decision = json.loads(Path(prepared['action_file']).with_name('review-decision.json').read_text())
    assert all(item['status'] == 'uncertain' for item in decision['items'])


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


@pytest.fixture
def acceptance_records(recorded_case, capsys):
    """Offline artifact fixture; its native tag does not represent a model call."""
    arguments, _, _ = recorded_case
    finance_preaudit_native_replay.main(arguments)
    prepared = json.loads(capsys.readouterr().out)
    action_file = Path(prepared['action_file'])
    action = json.loads(action_file.read_text())
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
    def check(*, expected_hash=None, review_hash=None):
        check_args = ['--verify' if x == '--prepare' else x for x in arguments]
        return finance_preaudit_native_replay.main(check_args + [
            '--expectations', str(expectations), '--expectations-sha256', expected_hash or finance_preaudit_native_replay.digest(expectations),
            '--independent-review', str(review), '--review-sha256', review_hash or (finance_preaudit_native_replay.digest(review) if review.exists() else initial_review_hash),
        ])
    return check, audit, expectations, review


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
        action = finance_preaudit_native_replay.read(manifest['action_file'])
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
    monkeypatch.setattr(codex_review_worker, 'REVIEW_SETTINGS', production_config.review_settings(provider))
    arguments, _, _ = recorded_case
    assert finance_preaudit_native_replay.main(arguments) == 0
    summary = json.loads(capsys.readouterr().out)
    manifest_file = Path(summary["audit"]) / "prepare.json"
    manifest = json.loads(manifest_file.read_text())
    assert manifest["review_configuration"] == production_config.review_settings(provider)
    other = 'openai' if provider == 'deepseek' else 'deepseek'
    manifest["review_configuration"] = production_config.review_settings(other)
    manifest_file.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="review_model_identity_changed"):
        finance_preaudit_native_replay.main(["--run" if arg == "--prepare" else arg for arg in arguments],
                                           executor=lambda *args: pytest.fail("changed provider started a model"))
    assert not manifest_file.with_name("worker-result.json").exists()
