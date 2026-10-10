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
