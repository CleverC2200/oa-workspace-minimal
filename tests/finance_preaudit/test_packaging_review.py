import io
import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import packaging_review as reviewer
from tests.finance_preaudit.test_flow_engine_packaging import _engine, WORKFLOW_KEY


def action(tmp_path, *, excel=False):
    root = tmp_path / 'runtime' / 'finance_preaudit_flows'
    result = _engine(root, excel=excel).next_action(WORKFLOW_KEY)
    return Path(result['action_file'])


def draft(packet):
    return {'overall': 'pass', 'items': [
        {'item_key': item['item_key'], 'status': 'pass', 'conclusion': '测试结论',
         'evidence_refs': [], 'missing_evidence': []} for item in packet['items']],
        'excel_comparison': {'status': 'not_applicable', 'resource_id': None, 'mappings': []},
        'supplementary_checks': [],
        'visual_review': {'outcome': 'all_non_packaging', 'pages': [
            {'page_ref': ref, 'classification': 'non_packaging', 'package_version_id': None, 'item_keys': []}
            for ref in packet['packaging_visual_contract']['page_refs']]}}


@pytest.mark.parametrize('excel', [False, True])
def test_schema_and_unique_fence_preserve_business_fields(tmp_path, excel):
    path = action(tmp_path, excel=excel)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    schema = reviewer.draft_schema(packet)
    body = draft(packet)
    raw = json.dumps(body, ensure_ascii=False)
    assert reviewer.parse_draft('说明\n```json\n' + raw + '\n```', schema) == body
    assert reviewer.parse_draft(raw, schema) == body
    with pytest.raises(ValueError):
        reviewer.parse_draft('```json\n' + raw + '\n```\n```json\n' + raw + '\n```', schema)
    with pytest.raises(ValueError):
        reviewer.parse_draft('{}', schema)
    with pytest.raises(ValueError, match='review_schema_empty_object'):
        reviewer.check_provider_schema({'type': 'array', 'items': {'type': 'object', 'properties': {}}})


def fake_process(monkeypatch, body, *, native_home, after=None):
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'unit-test-key')
    monkeypatch.setenv('OPENAI_API_KEY', 'host-key-not-for-review')
    monkeypatch.setenv('CODEX_API_KEY', 'host-key-not-for-review')
    native_home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(native_home))
    kwargs_cwd = None
    class Process:
        pid = 12345
        returncode = 0
        stderr = io.StringIO('original stderr\n')
        def __init__(self, command, **kwargs):
            nonlocal kwargs_cwd
            kwargs_cwd = kwargs['cwd']
            assert '--ephemeral' not in command
            assert 'OA_REVIEW_API_KEY' not in kwargs['env']
            assert 'OPENAI_API_KEY' not in kwargs['env'] and 'CODEX_API_KEY' not in kwargs['env']
            assert not list(Path(kwargs['env']['CODEX_HOME']).iterdir())
            self.target = Path(command[command.index('--output-last-message')+1])
            self.stdout = kwargs['stdout']
            self.home = Path(kwargs['env']['CODEX_HOME'])
            self.store = Path(next(value.split('=', 1)[1].strip(chr(34)) for value in command if value.startswith('sqlite_home=')))
        def communicate(self, *, input, timeout):
            assert 'view_image' in input and timeout == 60
            self.stdout.write('{"type":"thread.started","thread_id":"00000000-0000-0000-0000-000000000001"}\n{"type":"turn.completed"}\n')
            self.stdout.flush()
            self.target.write_text(body)
            native = self.home / 'sessions' / 'own.jsonl'
            native.parent.mkdir()
            native.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': '00000000-0000-0000-0000-000000000001', 'cwd': str(kwargs_cwd)}}) + '\n' + '{"type":"response_item","payload":{"type":"function_call","name":"view_image"}}\n')
            import sqlite3
            self.store.mkdir()
            with sqlite3.connect(self.store / 'state_5.sqlite') as database:
                database.execute('CREATE TABLE threads (id TEXT, cwd TEXT, rollout_path TEXT)')
                database.execute('INSERT INTO threads VALUES (?, ?, ?)', ('00000000-0000-0000-0000-000000000001', str(kwargs_cwd), str(native)))
            if after: after()
        def poll(self): return self.returncode
    monkeypatch.setattr(reviewer.worker.subprocess, 'Popen', Process)


def test_official_entry_retains_raw_receipts_and_binds_protocol_only(tmp_path, monkeypatch):
    path = action(tmp_path)
    before = {p.name: p.read_bytes() for p in path.parent.glob('*.json')}
    packet = json.loads(path.with_name('review-packet.json').read_text())
    body = draft(packet)
    raw = '说明\n```json\n' + json.dumps(body, ensure_ascii=False) + '\n```'
    fake_process(monkeypatch, raw, native_home=tmp_path / 'native-auth')
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert result['status'] == 'draft_ready' and result['write_state'] == 'not_requested'
    assert result['semantic_acceptance'] == 'not_checked'
    directory = Path(result['evidence_directory'])
    assert directory.joinpath('decision.json').read_text() == raw
    assert directory.joinpath('stderr.log').read_text() == 'original stderr\n'
    assert result['tool_receipt_files'] == ['tool-receipts/00000000-0000-0000-0000-000000000001.jsonl']
    bound = json.loads(Path(result['decision_file']).read_text())
    assert {key: bound[key] for key in body} == body
    assert {p.name: p.read_bytes() for p in path.parent.glob('*.json')} == before


@pytest.mark.parametrize('failure', ['malformed', 'frozen_changed'])
def test_failure_keeps_original_output_without_mutating_attempt(tmp_path, monkeypatch, failure):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    raw = 'not json' if failure == 'malformed' else json.dumps(draft(packet))
    fake_process(monkeypatch, raw, native_home=tmp_path / 'native-auth', after=(lambda: path.with_name('material.json').write_text('{}'))
                 if failure == 'frozen_changed' else None)
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert result['status'] == 'failed' and result['decision_file'] is None
    assert Path(result['evidence_directory']).joinpath('decision.json').read_text() == raw
    assert not path.with_name('decision-rejection.json').exists()


def test_naked_json_plus_fenced_json_is_ambiguous(tmp_path):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    raw = json.dumps(draft(packet))
    with pytest.raises(ValueError, match='packaging_review_json_ambiguous'):
        reviewer.parse_draft(raw + '\n```json\n' + raw + '\n```', reviewer.draft_schema(packet))


@pytest.mark.parametrize('classification,excel', [('packaging_layout', False), ('packaging_layout', True), ('uncertain', False)])
def test_draft_shapes_reach_existing_completion_consumer(tmp_path, classification, excel):
    from tests.finance_preaudit.test_flow_engine_packaging import _decision, _passing_items
    engine = _engine(tmp_path / 'runtime' / 'finance_preaudit_flows', excel=excel)
    current = engine.next_action(WORKFLOW_KEY)
    path = Path(current['action_file'])
    packet = json.loads(path.with_name('review-packet.json').read_text())
    items = _passing_items()
    visual = None
    if classification == 'uncertain':
        for item in items:
            item.update(status='uncertain', missing_evidence=['material:prepared.images.page-1.page-1', '关键文字不清'])
        visual = {'outcome':'reviewed', 'pages':[{'page_ref':'material:prepared.images.page-1.page-1',
                  'classification':'uncertain','package_version_id':None,'item_keys':[]}]}
    original = _decision(current, items, excel=excel, visual_review=visual)
    body = {key:original[key] for key in ['overall', 'items', 'excel_comparison', 'visual_review']}
    body['supplementary_checks'] = []
    parsed = reviewer.parse_draft(json.dumps(body), reviewer.draft_schema(packet))
    bound = reviewer.review_decision.bind_frozen_fields(path, parsed)
    target = tmp_path / 'consumer-decision.json'
    target.write_text(json.dumps(bound))
    # Existing preview consumer, offline material and no submit port.
    result = engine.complete_review(path, target)
    assert result['write_state'] == 'not_requested'
    assert result['business_state'] == ('needs_human' if classification == 'uncertain' else 'pass')


def test_packaging_receipt_records_current_model_without_deepseek_key(tmp_path, monkeypatch):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    fake_process(monkeypatch, json.dumps(draft(packet)), native_home=tmp_path / 'native-auth')
    monkeypatch.delenv('OA_REVIEW_API_KEY', raising=False)
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert result['status'] == 'draft_ready'
    assert result['model'] == 'gpt-6.1-sol' and result['reasoning_effort'] == 'medium'
    assert result['model_provider'] == 'openai' and result['requested_service_tier'] == 'default'
    assert not Path(result['evidence_directory']).joinpath('auth.json').exists()


def test_audit_timeout_retains_events_and_reaps_real_child(tmp_path, monkeypatch):
    import sys
    from tests.finance_preaudit.test_process_pool import _install_fake_codex_tree, _assert_fake_codex_tree_reaped
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'unit-test-key')
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    pid_file, fake_codex, marker = _install_fake_codex_tree(tmp_path, monkeypatch, ignore_child_sigterm=False)
    directory = tmp_path / 'audit'; directory.mkdir(mode=0o700)
    result = reviewer.worker._execute_codex_raw(tmp_path, path, 1, read_only=True, stderr_summary={},
        schema_builder=lambda _: reviewer.draft_schema(packet), prompt_builder=lambda _: '只读测试', audit_directory=directory)
    assert result.error_code == 'codex_process_timeout'
    assert directory.joinpath('events.jsonl').is_file() and directory.joinpath('stderr.log').is_file()
    _assert_fake_codex_tree_reaped(pid_file, fake_codex, marker)


def test_binding_failure_still_returns_completed_process_receipt(tmp_path, monkeypatch):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    fake_process(monkeypatch, json.dumps(draft(packet)), native_home=tmp_path / 'native-auth')
    def fail(*_args): raise ValueError('frozen_packet_changed')
    monkeypatch.setattr(reviewer.review_decision, 'bind_frozen_fields', fail)
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert result['error_code'] == 'packaging_review_assembly_failed'
    assert result['exit_code'] == 0 and result['decision_file'] is None
    assert Path(result['evidence_directory']).joinpath('review-receipt.json').is_file()


@pytest.mark.parametrize("native_content", ["", '{"type":"session_meta","payload":{"id":"foreign","cwd":"foreign"}}\nprivate other chat body'])
def test_native_receipt_mismatch_fails_without_copying_other_chat(tmp_path, monkeypatch, native_content):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    native_home = tmp_path / 'native-auth'
    fake_process(monkeypatch, json.dumps(draft(packet)), native_home=native_home,
                 after=lambda: native_home.joinpath('sessions/own.jsonl').write_text(native_content))
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert result['status'] == 'failed' and result['error_code'] == 'codex_tool_receipt_unavailable'
    assert result['tool_receipt_files'] == [] and result['decision_file'] is None
    assert not Path(result['evidence_directory']).joinpath('tool-receipts').exists()
