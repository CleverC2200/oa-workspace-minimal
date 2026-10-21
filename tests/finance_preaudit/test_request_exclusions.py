from copy import deepcopy
import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import flow_engine, reconciliation, request_exclusions
from tests.finance_preaudit.test_flow_engine_replay import _candidates, _decision, _engine, _grant, _material, _success
from tests.finance_preaudit.test_reconciliation import _state


def _closed_submission(project_root: Path) -> tuple[Path, list[dict], dict]:
    root = project_root / 'runtime/finance_preaudit_flows'

    def collect(request_id, attempt_root):
        material = _material(request_id)
        attachment = attempt_root / 'attachments' / 'evidence.bin'
        attachment.write_bytes(b'recorded evidence')
        material['resources'] = [{'request_id': request_id, 'resource_id': 'resource',
            'source_resource_id': 'source', 'source_system': 'oa', 'local_readonly_path': str(attachment)}]
        material['prepared']['manifest'] = [{'resource_id': 'resource', 'status': 'unsupported_file'}]
        return material

    engine = _engine(root, collect_material=collect, authorization_source=_grant, submit_frozen=_success)
    action = engine.next_action('travel_expense', mode='controlled_write')
    decision = project_root / 'decision.json'
    json_artifact.write_json_atomic(decision, _decision(action))
    closed = engine.complete_review(Path(action['action_file']), decision)
    assert closed['write_state'] == 'submitted'
    path = Path(action['action_file'])
    path.with_name('attachments').joinpath('evidence.bin').unlink()
    rows = [{'request_id': 'recorded-request', 'workflow_key': 'travel_expense',
             'received_at': '2026-08-23T09:00:00+08:00'}]
    raw = reconciliation.reconcile(root, pending_rows=rows, handled_rows=[])
    assert raw['continuation']['blocking_reasons'] == {'identity_conflict': 1}
    return path, rows, raw


def _collecting_claim(project_root: Path) -> tuple[Path, list[dict], dict]:
    root = project_root / 'runtime/finance_preaudit_flows'

    def interrupted(*_args):
        raise SystemExit('recorded_preparation_interruption')

    engine = _engine(root, collect_material=interrupted, authorization_source=_grant, submit_frozen=_success)
    with pytest.raises(SystemExit, match='recorded_preparation_interruption'):
        engine.next_action('travel_expense', mode='controlled_write')
    path = next(root.glob('*/cases/*/attempt-1/action.json'))
    rows = _candidates()
    raw = reconciliation.reconcile(root, pending_rows=rows, handled_rows=[])
    assert raw['findings'][0]['local_execution_state'] == 'collecting'
    return path, rows, raw


def test_exclusion_projects_only_its_request_without_restoring_historical_verification(tmp_path):
    action, rows, raw = _closed_submission(tmp_path)
    before = json.dumps(raw, sort_keys=True)
    original_files = {path: path.read_bytes() for path in action.parent.iterdir() if path.is_file()}
    state = json.loads(action.with_name('state.json').read_text())
    assert flow_engine.FlowEngine.confirmed_submission(tmp_path / 'runtime/finance_preaudit_flows', state) is False
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=710)
    projected = scope.project(raw)
    assert projected['continuation'] == {'status': 'ready', 'scope': 'none', 'isolated_pending': 0, 'blocking_reasons': {}}
    assert projected['scope_excluded_pending'] == 1
    assert projected['quarantine']['actionable_pending'] == 0
    assert projected['quarantine']['pending_quarantined'] == raw['quarantine']['pending_quarantined']
    assert projected['findings'] == []
    assert projected['execution_exclusions']['raw_continuation'] == raw['continuation']
    assert projected['execution_exclusions']['historical_submission_verified'] is False
    assert json.dumps(raw, sort_keys=True) == before
    assert all(path.read_bytes() == data for path, data in original_files.items())
    assert 'recorded-request' not in json.dumps(scope.receipt())
    assert scope.filter_candidates(rows + [{**rows[0], 'received_at': '2026-08-24T09:00:00+08:00'},
        {**rows[0], 'request_id': 'other'}]) == [{**rows[0], 'request_id': 'other'}]
    assert flow_engine.FlowEngine.confirmed_submission(tmp_path / 'runtime/finance_preaudit_flows', state) is False


def test_exclusion_preserves_other_identity_conflict_and_unknown_write_blockers(tmp_path):
    action, _, raw = _closed_submission(tmp_path)
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=710)
    other = {**raw['findings'][0], 'item_key': 'a' * 64}
    raw['findings'].append(other)
    raw['counts']['identity_conflict'] += 1
    raw['recommendations']['manual_state_review'] += 1
    raw['local_cases'] += 1
    raw['continuation']['blocking_reasons'] = {'identity_conflict': 2, 'pending_write_state_unresolved': 1}
    projected = scope.project(raw)
    assert projected['findings'] == [other]
    assert projected['continuation']['status'] == 'blocked'
    assert projected['continuation']['blocking_reasons'] == {'identity_conflict': 1, 'pending_write_state_unresolved': 1}


def test_exclusion_stops_counting_a_request_that_is_no_longer_pending(tmp_path):
    action, _, _ = _closed_submission(tmp_path)
    raw = reconciliation.reconcile(tmp_path / 'runtime/finance_preaudit_flows', pending_rows=[], handled_rows=[])
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=710)
    projected = scope.project(raw)
    assert projected['scope_excluded_pending'] == 0
    assert projected['quarantine'] == raw['quarantine']
    assert projected['findings'] == raw['findings']


def test_exclusion_counts_each_pending_received_version_without_changing_raw(tmp_path):
    action, rows, _ = _closed_submission(tmp_path)
    rows += [{**rows[0], 'received_at': '2026-08-24T09:00:00+08:00'},
             {**rows[0], 'request_id': 'other-request'}]
    raw = reconciliation.reconcile(tmp_path / 'runtime/finance_preaudit_flows', pending_rows=rows, handled_rows=[])
    before = deepcopy(raw)
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=710)
    projected = scope.project(raw)
    assert projected['scope_excluded_pending'] == 2
    assert projected['quarantine']['actionable_pending'] == 1
    assert projected['quarantine']['pending_quarantined'] == raw['quarantine']['pending_quarantined']
    assert projected['continuation']['status'] == 'ready'
    assert scope.filter_candidates(rows) == [rows[2]]
    assert raw == before


@pytest.mark.parametrize('damage', ['missing', 'truncated', 'unmatched', 'cross_workflow'])
def test_exclusion_refuses_to_guess_pending_counts_without_complete_matching_details(tmp_path, damage):
    action, _, raw = _closed_submission(tmp_path)
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=710)
    if damage == 'missing':
        raw.pop('pending_dispositions')
    elif damage == 'truncated':
        raw['pending_dispositions']['items'] = []
    elif damage == 'unmatched':
        raw['pending_dispositions']['items'][0]['item_key'] = 'f' * 64
    else:
        raw['pending_dispositions']['items'][0]['workflow_key'] = 'general_expense'
    with pytest.raises(ValueError, match='request_exclusion_untrusted'):
        scope.project(raw)


@pytest.mark.parametrize('name', ['action.json', 'state.json', 'material.json', 'review-packet.json',
    'review-decision.json', 'remark.txt', 'submission-envelope.json', 'execution-receipt.json'])
def test_exclusion_rejects_modified_main_evidence(tmp_path, name):
    action, _, _ = _closed_submission(tmp_path)
    action.with_name(name).write_text('{}')
    with pytest.raises(ValueError, match='request_exclusion_untrusted'):
        request_exclusions.Scope.bind(tmp_path, [action], issue=710)


def test_exclusion_verify_rejects_replaced_files_even_when_bytes_match(tmp_path):
    action, _, _ = _closed_submission(tmp_path)
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=710)
    replacement = action.with_name('replacement.json')
    replacement.write_bytes(action.read_bytes())
    replacement.replace(action)
    with pytest.raises(ValueError, match='request_exclusion_untrusted'):
        scope.verify()


@pytest.mark.parametrize('write_state', ['unknown', 'submitting'])
def test_exclusion_rejects_another_local_version_with_unresolved_write(tmp_path, write_state):
    action, _, _ = _closed_submission(tmp_path)
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=710)
    _state(tmp_path / 'runtime/finance_preaudit_flows', 'recorded-request', '2026-08-24T09:00:00+08:00',
           write_state=write_state)
    with pytest.raises(ValueError, match='request_exclusion_untrusted'):
        scope.verify()


@pytest.mark.parametrize('row', [
    {'requestId': 'recorded-request', 'flow_key': 'general_expense'},
    {'requestId': 'recorded-request', 'flow_key': 'travel_expense', 'request_id': 'other'},
    {'requestId': 'recorded-request'},
])
def test_exclusion_rejects_ambiguous_or_cross_workflow_candidates(tmp_path, row):
    action, _, _ = _closed_submission(tmp_path)
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=710)
    with pytest.raises(ValueError, match='request_exclusion_untrusted'):
        scope.filter_candidates([row])


def test_exclusion_requires_explicit_nonempty_binding_and_rejects_double_projection(tmp_path):
    with pytest.raises(ValueError, match='request_exclusion_untrusted'):
        request_exclusions.Scope.bind(tmp_path, [], issue=710)
    action, _, raw = _closed_submission(tmp_path)
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=710)
    original = deepcopy(scope.receipt())
    scope.receipt()['items'].clear()
    assert scope.receipt() == original
    with pytest.raises(ValueError, match='request_exclusion_untrusted'):
        scope.project(scope.project(raw))


def test_exclusion_rejects_relative_action_and_unexplained_blocked_continuation(tmp_path, monkeypatch):
    action, _, raw = _closed_submission(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match='request_exclusion_untrusted'):
        request_exclusions.Scope.bind(tmp_path, [action.relative_to(tmp_path)], issue=710)
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=710)
    raw['continuation']['blocking_reasons'] = {}
    with pytest.raises(ValueError, match='request_exclusion_untrusted'):
        scope.project(raw)


@pytest.mark.parametrize('recovery_receipt', [False, True])
def test_exclusion_scopes_a_collecting_claim_without_validating_recovery_or_materials(tmp_path, recovery_receipt):
    action, rows, raw = _collecting_claim(tmp_path)
    if recovery_receipt:
        action.with_name('recovery-receipt.json').write_text('{"failure_code":"codex_review_interrupted"}')
    rows += [{**rows[0], 'received_at': '2026-08-24T09:00:00+08:00'},
             {**rows[0], 'request_id': 'other-request'}]
    raw = reconciliation.reconcile(tmp_path / 'runtime/finance_preaudit_flows', pending_rows=rows, handled_rows=[])
    before = deepcopy(raw)
    original_files = {path: path.read_bytes() for path in action.parent.iterdir() if path.is_file()}
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=714)
    projected = scope.project(raw)
    assert projected['scope_excluded_pending'] == 2
    assert projected['quarantine']['actionable_pending'] == 1
    assert projected['findings'] == [] and projected['local_cases'] == 0
    assert projected['counts'] == {} and projected['recommendations'] == {}
    assert scope.filter_candidates(rows) == [rows[2]]
    item = scope.receipt()['items'][0]
    assert item['source_execution_state'] == 'collecting'
    assert item['source_write_state'] == 'not_requested' and item['source_write_authorized'] is False
    assert {Path(pin['path']).name for pin in item['pins']} == {'action.json', 'state.json'} | (
        {'recovery-receipt.json'} if recovery_receipt else set())
    assert scope.receipt()['historical_submission_verified'] is False
    assert raw == before
    assert {path: path.read_bytes() for path in action.parent.iterdir() if path.is_file()} == original_files


@pytest.mark.parametrize('artifact', ['review-decision.json', 'remark.txt', 'submission-envelope.json',
    'execution-receipt.json', 'writer-boundary.json'])
@pytest.mark.parametrize('after_bind', [False, True])
def test_collecting_exclusion_rejects_existing_or_new_write_artifacts_including_broken_links(tmp_path, artifact, after_bind):
    action, _, raw = _collecting_claim(tmp_path)
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=714) if after_bind else None
    action.with_name(artifact).symlink_to(action.with_name('missing-artifact'))
    with pytest.raises(ValueError, match='request_exclusion_untrusted'):
        if scope is None:
            request_exclusions.Scope.bind(tmp_path, [action], issue=714)
        else:
            scope.project(raw)


@pytest.mark.parametrize('change', ['new_attempt', 'new_receipt', 'changed_receipt', 'authorized', 'unknown', 'submitting'])
def test_collecting_exclusion_rejects_progress_or_changed_write_state(tmp_path, change):
    action, _, _ = _collecting_claim(tmp_path)
    recovery = action.with_name('recovery-receipt.json')
    if change == 'changed_receipt':
        recovery.write_text('{}')
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=714)
    if change == 'new_attempt':
        action.parent.with_name('attempt-2').mkdir()
    elif change in {'new_receipt', 'changed_receipt'}:
        recovery.write_text('{"new":true}')
    else:
        state = json.loads(action.with_name('state.json').read_text())
        if change == 'authorized':
            state['write_authorized'] = True
        else:
            state['write_state'] = change
        action.with_name('state.json').write_text(json.dumps(state))
    with pytest.raises(ValueError, match='request_exclusion_untrusted'):
        scope.verify()


def test_collecting_exclusion_preserves_other_blocking_reasons_and_rejects_stale_claim(tmp_path):
    action, _, raw = _collecting_claim(tmp_path)
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=714)
    raw['continuation'].update(status='blocked', scope='controller',
                               blocking_reasons={'pending_write_state_unresolved': 1})
    projected = scope.project(raw)
    assert projected['continuation']['blocking_reasons'] == {'pending_write_state_unresolved': 1}
    assert projected['continuation']['status'] == 'blocked'
    state = json.loads(action.with_name('state.json').read_text())
    _state(tmp_path / 'runtime/finance_preaudit_flows', state['request_id'], state['received_at'],
           attempt=2, execution_state='collecting')
    with pytest.raises(ValueError, match='request_exclusion_untrusted'):
        request_exclusions.Scope.bind(tmp_path, [action], issue=714)
