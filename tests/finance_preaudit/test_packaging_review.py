import io
import json
import os
import stat
import tomllib
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


def fake_process(monkeypatch, body, *, native_home, after=None, inspect_start=None):
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'unit-test-key')
    monkeypatch.setenv('OPENAI_API_KEY', 'host-key-not-for-review')
    monkeypatch.setenv('CODEX_API_KEY', 'host-key-not-for-review')
    native_home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(native_home))
    kwargs_cwd = None
    class Process:
        pid = 12345
        returncode = 0
        stderr = io.TextIOWrapper(io.BytesIO(b'original stderr\n'), encoding="utf-8")
        def __init__(self, command, **kwargs):
            nonlocal kwargs_cwd
            kwargs_cwd = kwargs['cwd']
            assert '--ephemeral' not in command
            assert kwargs['env']['OA_REVIEW_API_KEY'] == 'unit-test-key'
            assert 'OPENAI_API_KEY' not in kwargs['env'] and 'CODEX_API_KEY' not in kwargs['env']
            assert not list(Path(kwargs['env']['CODEX_HOME']).iterdir())
            self.target = Path(command[command.index('--output-last-message')+1])
            self.stdout = kwargs['stdout']
            self.home = Path(kwargs['env']['CODEX_HOME'])
            self.store = Path(next(value.split('=', 1)[1].strip(chr(34)) for value in command if value.startswith('sqlite_home=')))
            self.command = command
            if inspect_start: inspect_start(command, self.target.parent)
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
            if after: after(self.home)
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
    assert result['contract_validation'] == 'passed'
    assert result['decision_validation_code'] is None
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
    fake_process(monkeypatch, raw, native_home=tmp_path / 'native-auth', after=(lambda _home: path.with_name('material.json').write_text('{}'))
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


def test_packaging_receipt_records_current_deepseek_identity(tmp_path, monkeypatch):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    fake_process(monkeypatch, json.dumps(draft(packet)), native_home=tmp_path / 'native-auth')
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert result['status'] == 'draft_ready'
    assert result['model'] == 'deepseek-flash' and result['reasoning_effort'] == 'high'
    assert result['model_provider'] == 'oa_deepseek' and result['requested_service_tier'] == 'default'
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
                 after=lambda review_home: review_home.joinpath('sessions/own.jsonl').write_text(native_content))
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert result['status'] == 'failed' and result['error_code'] == 'codex_tool_receipt_unavailable'
    assert result['tool_receipt_files'] == [] and result['decision_file'] is None
    assert not Path(result['evidence_directory']).joinpath('tool-receipts').exists()


@pytest.mark.parametrize('version_id', ['包装-A', '', 'v' * 81, 'version-1\n'])
def test_draft_schema_rejects_invalid_package_version_id(tmp_path, version_id):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    body = draft(packet)
    body['visual_review']['pages'][0]['package_version_id'] = version_id
    with pytest.raises(ValueError, match='packaging_review_schema_invalid'):
        reviewer.parse_draft(json.dumps(body), reviewer.draft_schema(packet))


def test_native_draft_schema_uses_regex_without_unsupported_lookaround(tmp_path):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    schema = reviewer.draft_schema(packet)
    pattern = schema['properties']['visual_review']['properties']['pages']['items']['properties']['package_version_id']['pattern']
    assert '(?' not in pattern


def test_provider_schema_rejects_known_unsupported_lookaround_before_execution():
    with pytest.raises(ValueError, match='review_schema_unsupported_pattern'):
        reviewer.check_provider_schema({'type': 'string', 'pattern': r'^[A-Z]+(?![\s\S])'})


def test_readonly_consumer_validation_preserves_bound_decision_and_attempt(tmp_path):
    from copy import deepcopy
    from apps.oa_finance_preaudit import decision_validation, flow_engine
    from tests.finance_preaudit.test_flow_engine_packaging import _decision, _passing_items

    root = tmp_path / 'runtime' / 'finance_preaudit_flows'
    calls = []
    current = _engine(root, calls=calls).next_action(WORKFLOW_KEY)
    path = Path(current['action_file'])
    bound = _decision(current, _passing_items())
    original = deepcopy(bound)
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}

    def unavailable(*_args, **_kwargs):
        pytest.fail('readonly validation called an authorization or OA port')

    reader = flow_engine.FlowEngine(
        root=root, discover_candidates=unavailable, collect_material=unavailable,
        authorization_source=unavailable, authorization_verifier=unavailable,
        submit_frozen=unavailable,
    )
    reader.validate_review_decision(path, bound)
    assert bound == original
    assert calls == []
    assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before

    bound['visual_review']['pages'].append(deepcopy(bound['visual_review']['pages'][0]))
    invalid_original = deepcopy(bound)
    with pytest.raises(decision_validation.InvalidDecision, match='review_decision_invalid'):
        reader.validate_review_decision(path, bound)
    assert bound == invalid_original
    assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('invalid,validation_code', [
    ('duplicate_page', 'packaging_visual_contract'),
    ('item_order', 'item_values'),
    ('overall', 'packaging_overall_mismatch'),
    ('excel', 'unspecified'),
    ('supplementary', 'packaging_supplementary_checks'),
])
def test_schema_valid_contract_failure_never_becomes_ready(tmp_path, monkeypatch, invalid, validation_code):
    from copy import deepcopy
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    body = draft(packet)
    if invalid == 'duplicate_page':
        body['visual_review']['pages'].append(deepcopy(body['visual_review']['pages'][0]))
    elif invalid == 'item_order':
        body['items'][0], body['items'][1] = body['items'][1], body['items'][0]
    elif invalid == 'overall':
        body['overall'] = 'needs_human'
    elif invalid == 'excel':
        body['excel_comparison']['status'] = 'not_provided'
    else:
        body['supplementary_checks'] = [{
            'page_ref': body['visual_review']['pages'][0]['page_ref'],
            'status': 'uncertain', 'conclusion': '测试补充异常',
        }]
    raw = json.dumps(body, ensure_ascii=False)
    assert reviewer.parse_draft(raw, reviewer.draft_schema(packet)) == body
    before = {str(p.relative_to(path.parent)): p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
    fake_process(monkeypatch, raw, native_home=tmp_path / 'native-auth')
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert result['status'] == 'failed'
    assert result['error_code'] == 'packaging_review_contract_invalid'
    assert result['contract_validation'] == 'failed'
    assert result['decision_validation_code'] == validation_code
    assert result['decision_file'] is None
    assert Path(result['evidence_directory']).joinpath('decision.json').read_text() == raw
    assert {str(p.relative_to(path.parent)): p.read_bytes() for p in path.parent.rglob('*') if p.is_file()} == before


def test_native_fifo_receipt_returns_failure_within_finite_host_deadline(tmp_path):
    import subprocess
    import sys
    code = '''
import json
import os
from pathlib import Path
import pytest
from tests.finance_preaudit.test_packaging_review import action, draft, fake_process
from apps.oa_finance_preaudit import packaging_review as reviewer
root = Path(__import__('sys').argv[1])
path = action(root)
packet = json.loads(path.with_name('review-packet.json').read_text())
home = root / 'native-auth'
def replace_receipt(home):
    target = home / 'sessions' / 'own.jsonl'
    target.unlink()
    os.mkfifo(target)
with pytest.MonkeyPatch.context() as patch:
    fake_process(patch, json.dumps(draft(packet)), native_home=home, after=replace_receipt)
    result = reviewer.review(path, project_root=root, timeout_seconds=60)
print(json.dumps(result))
'''
    try:
        completed = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                                   capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        pytest.fail('native FIFO receipt blocked the review host beyond its finite deadline')
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result['status'] == 'failed'
    assert result['error_code'] == 'codex_tool_receipt_unavailable'
    assert result['tool_receipt_files'] == [] and result['decision_file'] is None


@pytest.mark.parametrize('damage', ['missing', 'directory', 'symlink', 'oversized', 'oversized_header', 'malformed_header'])
def test_damaged_native_receipt_fails_before_draft_ready(tmp_path, monkeypatch, damage):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    home = tmp_path / 'native-auth'
    raw = json.dumps(draft(packet))
    def damage_receipt(home):
        native = home / 'sessions' / 'own.jsonl'
        if damage == 'oversized':
            with native.open('ab') as stream:
                stream.truncate(32 * 1024 * 1024 + 1)
        elif damage == 'oversized_header':
            native.write_bytes(b' ' * (1024 * 1024 + 1) + b'\n')
        elif damage == 'malformed_header':
            native.write_text('not json\n')
        else:
            native.unlink()
            if damage == 'directory':
                native.mkdir()
            elif damage == 'symlink':
                unrelated = home / 'sessions' / 'unrelated.jsonl'
                unrelated.write_text('unrelated private content')
                native.symlink_to(unrelated)
    fake_process(monkeypatch, raw, native_home=home, after=damage_receipt)
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert result['status'] == 'failed' and result['error_code'] == 'codex_tool_receipt_unavailable'
    assert result['tool_receipt_files'] == [] and result['decision_file'] is None
    assert Path(result['evidence_directory']).joinpath('decision.json').read_text() == raw


def test_native_receipt_replaced_after_open_is_not_retained(tmp_path, monkeypatch):
    import os
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    home = tmp_path / 'native-auth'
    fake_process(monkeypatch, json.dumps(draft(packet)), native_home=home)
    original_open = os.open
    replaced = []
    def replace_after_open(file, flags, *args, **kwargs):
        descriptor = original_open(file, flags, *args, **kwargs)
        if file == 'own.jsonl':
            native = next((tmp_path / 'runtime/control/packaging-reviews').glob('*/codex-home/sessions/own.jsonl'))
            replacement = native.with_name('replacement.jsonl')
            replacement.write_bytes(native.read_bytes())
            os.replace(replacement, native)
            replaced.append(True)
        return descriptor
    monkeypatch.setattr(os, 'open', replace_after_open)
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert replaced == [True]
    assert result['status'] == 'failed' and result['error_code'] == 'codex_tool_receipt_unavailable'
    assert result['tool_receipt_files'] == [] and result['decision_file'] is None


def test_packaging_review_grants_only_its_private_visual_aux_directory(tmp_path, monkeypatch):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    before = {p.name:p.read_bytes() for p in path.parent.glob('*.json')}
    seen = []
    def observe(command, directory):
        aux = directory / 'visual-aux'
        metadata = aux.lstat()
        assert stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid()
        assert stat.S_IMODE(metadata.st_mode) == 0o700
        profile = tomllib.loads(next(v for v in command if v.startswith('permissions.oa-reviewer=')))['permissions']['oa-reviewer']
        assert profile['extends'] == ':read-only'
        assert {k:v for k,v in profile['filesystem'].items() if v == 'write'} == {str(aux):'write'}
        assert profile['filesystem'][str(tmp_path)] == 'read'
        for protected in (path, path.with_name('state.json'), path.with_name('material.json'),
                          path.with_name('review-packet.json'), directory/'decision-schema.json',
                          directory/'events.jsonl', directory/'other-visual-aux', tmp_path/'other-audit/visual-aux'):
            assert not protected.is_relative_to(aux)
            assert str(protected) not in {k for k,v in profile['filesystem'].items() if v == 'write'}
        assert command[command.index('--model')+1] == 'deepseek-flash'
        assert 'model_reasoning_effort="high"' in command and 'service_tier="default"' in command
        seen.append(aux)
    fake_process(monkeypatch, json.dumps(draft(packet)), native_home=tmp_path/'native-auth', inspect_start=observe)
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert result['status'] == 'draft_ready' and result['business_fields_changed'] is False
    assert seen == [Path(result['evidence_directory'])/'visual-aux']
    assert {p.name:p.read_bytes() for p in path.parent.glob('*.json')} == before
    prompt = Path(result['evidence_directory']).joinpath('prompt.txt').read_text()
    assert str(seen[0]) in prompt and 'Base64' in prompt and 'view_image' in prompt and '无损' in prompt


@pytest.mark.parametrize('damage', ['mode', 'replacement', 'symlink'])
def test_packaging_review_rejects_changed_visual_aux_directory(tmp_path, monkeypatch, damage):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    before = {p.name:p.read_bytes() for p in path.parent.glob('*.json')}
    captured = []
    def observe(_command, directory):
        captured.append(directory/'visual-aux')
    def change(_home):
        aux = captured[0]
        if damage == 'mode':
            aux.chmod(0o777)
        else:
            aux.rename(aux.with_name('original-visual-aux'))
            if damage == 'replacement': aux.mkdir(mode=0o700)
            else: aux.symlink_to(tmp_path, target_is_directory=True)
    fake_process(monkeypatch, json.dumps(draft(packet)), native_home=tmp_path/'native-auth',
                 inspect_start=observe, after=change)
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert result['status'] == 'failed' and result['decision_file'] is None
    assert result['business_fields_changed'] is False
    assert {p.name:p.read_bytes() for p in path.parent.glob('*.json')} == before


def test_packaging_calls_have_distinct_visual_aux_write_scopes(tmp_path, monkeypatch):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    scopes = []
    def observe(command, directory):
        filesystem = tomllib.loads(next(v for v in command if v.startswith('permissions.oa-reviewer=')))['permissions']['oa-reviewer']['filesystem']
        scopes.append({k for k,v in filesystem.items() if v == 'write'})
        assert scopes[-1] == {str(directory/'visual-aux')}
    for call in range(2):
        fake_process(monkeypatch, json.dumps(draft(packet)), native_home=tmp_path/f'native-auth-{call}', inspect_start=observe)
        result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
        assert result['status'] == 'draft_ready'
    assert len(scopes) == 2 and scopes[0].isdisjoint(scopes[1])


def test_packaging_review_rejects_untrusted_aux_owner_before_native_launch(tmp_path, monkeypatch):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    launches = []
    fake_process(monkeypatch, json.dumps(draft(packet)), native_home=tmp_path/'native-auth',
                 inspect_start=lambda *_args: launches.append(True))
    current_uid = os.getuid()
    monkeypatch.setattr(reviewer.worker.os, 'getuid', lambda: current_uid + 1)
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert result['status'] == 'failed' and result['decision_file'] is None
    assert launches == []


@pytest.mark.parametrize('damage', ['mode', 'owner'])
def test_packaging_review_rejects_audit_parent_drift_at_completion(tmp_path, monkeypatch, damage):
    path = action(tmp_path)
    packet = json.loads(path.with_name('review-packet.json').read_text())
    before = {p.name:p.read_bytes() for p in path.parent.glob('*.json')}
    captured = []
    def observe(_command, directory):
        captured.append(directory)
    def change(_home):
        directory = captured[0]
        if damage == 'mode':
            directory.chmod(0o777)
        else:
            real_fstat = os.fstat
            parent_inode = directory.stat().st_ino
            def changed_owner(descriptor):
                metadata = real_fstat(descriptor)
                if metadata.st_ino == parent_inode:
                    values = list(metadata)
                    values[4] = metadata.st_uid + 1
                    return os.stat_result(values)
                return metadata
            monkeypatch.setattr(reviewer.worker.os, 'fstat', changed_owner)
        assert stat.S_IMODE((directory/'visual-aux').stat().st_mode) == 0o700
    fake_process(monkeypatch, json.dumps(draft(packet)), native_home=tmp_path/'native-auth',
                 inspect_start=observe, after=change)
    result = reviewer.review(path, project_root=tmp_path, timeout_seconds=60)
    assert result['status'] == 'failed' and result['decision_file'] is None
    assert result['business_fields_changed'] is False
    assert {p.name:p.read_bytes() for p in path.parent.glob('*.json')} == before


@pytest.fixture(autouse=True)
def fake_review_api_key(monkeypatch):
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-fake-key")
