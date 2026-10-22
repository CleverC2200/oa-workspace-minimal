from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import approval_lifecycle, controller_handoff, reconciliation, request_exclusions
from scripts import finance_preaudit_concurrency_probe as probe
from tests.finance_preaudit.test_flow_engine_replay import _candidates, _downloaded_material, _engine, _grant, _success
from tests.finance_preaudit.test_request_exclusions import _closed_submission, _collecting_claim


@pytest.fixture
def excluded_request(tmp_path, monkeypatch):
    action, rows, raw = _closed_submission(tmp_path)
    monkeypatch.setattr(probe, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(runtime_control, 'PROJECT_ROOT', tmp_path)
    return action, rows, raw


def selection(root, action):
    scope = request_exclusions.Scope.bind(root, [action], issue=710)
    return {'workflow_key': '*', 'assigned_identities': [], 'exclude_action_files': [str(action)],
            'exclusion_issue': 710, 'request_exclusions': scope.receipt()}


def pending(rows):
    return [{'requestId': row['request_id'], 'flow_key': row['workflow_key'], 'received_at': row['received_at'],
             'support_status': 'supported', 'can_submit': True} for row in rows]


@pytest.mark.parametrize('allowed', [False, True])
def test_prepare_filters_all_received_versions_before_fifo_and_recovery(excluded_request, monkeypatch, tmp_path, allowed):
    action, rows, _ = excluded_request
    original = {path: path.read_bytes() for path in action.parent.iterdir() if path.is_file()}
    live = pending(rows + [{**rows[0], 'received_at': '2026-08-24T09:00:00+08:00'}])
    other = {**rows[0], 'request_id': 'another-request', 'received_at': '2026-08-25T09:00:00+08:00'}
    if allowed:
        live.extend(pending([other]))
    calls = []
    def build(*, controlled, selected_identity):
        calls.append(selected_identity)
        assert controlled is True and selected_identity == (other['request_id'], other['received_at'])
        return _engine(tmp_path / 'runtime/finance_preaudit_flows', candidates=[other], authorization_source=_grant,
                       submit_frozen=_success)
    monkeypatch.setattr(probe.cli, '_live_pending_identities', lambda: list(live))
    monkeypatch.setattr(probe.cli, '_live_flow_engine', build)
    monkeypatch.setattr(probe, '_historical_write_authorization_failures', frozenset)
    monkeypatch.setattr(probe, '_local_quarantine_count', lambda: 0)
    monkeypatch.setattr(probe, '_local_quarantined_identities', frozenset)
    selected = selection(tmp_path, action)
    selected['recover_action_files'] = [str(action)]
    result = probe._prepare_one(selected)
    assert result['action'] == ('review_required' if allowed else 'no_action'), json.dumps(result)
    assert len(calls) == int(allowed)
    assert all(path.read_bytes() == data for path, data in original.items())
    assert len(list(action.parent.parent.glob('attempt-*'))) == 1


def test_prepare_rejects_a_different_parent_binding_before_reading_pending(excluded_request, monkeypatch, tmp_path):
    action, _, _ = excluded_request
    selected = selection(tmp_path, action)
    selected['request_exclusions']['issue'] = 711
    monkeypatch.setattr(probe.cli, '_live_pending_identities', lambda: pytest.fail('pending called after binding changed'))
    assert probe._prepare_one(selected)['action'] == 'failed'


def test_dispatcher_passes_frozen_exclusions_through_prepare_selection(excluded_request, monkeypatch, tmp_path):
    action, rows, _ = excluded_request
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=710)
    seen = []
    dispatcher = probe._run_dispatched_tasks
    def prepare(selected):
        seen.append(selected)
        return probe._prepare_one(selected)
    def dispatch(count, capacity, **kwargs):
        return dispatcher(count, capacity, **kwargs, renew_actions=lambda _: [], prepare_action=prepare,
            executor_factory=lambda size: ThreadPoolExecutor(max_workers=size), runtime_status=lambda: {'status': 'healthy'})
    monkeypatch.setattr(probe, '_run_dispatched_tasks', dispatch)
    monkeypatch.setattr(probe, '_preparation_executor', lambda: ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(probe.codex_review_worker, '_reviewer_environment', lambda: {})
    monkeypatch.setattr(probe.host_chat_review, 'selected', lambda: False)
    monkeypatch.setattr(probe.cli, '_live_pending_identities', lambda: pending(rows))
    monkeypatch.setattr(probe.cli, '_live_flow_engine', lambda **_: pytest.fail('excluded request reached engine'))
    monkeypatch.setattr(probe, '_historical_write_authorization_failures', frozenset)
    monkeypatch.setattr(probe, '_local_quarantine_count', lambda: 0)
    monkeypatch.setattr(probe, '_local_quarantined_identities', frozenset)
    result = probe._run_global_dispatched_tasks(1, 1, historical_unknown_identities=frozenset(),
        excluded_action_files=[action], exclusion_issue=710, exclusion_receipt=scope.receipt())
    assert result.failed == 0 and seen
    assert seen[0]['exclude_action_files'] == [str(action)]
    assert seen[0]['exclusion_issue'] == 710 and seen[0]['request_exclusions'] == scope.receipt()


def test_rolling_projects_execution_scope_and_preserves_raw_blocker(excluded_request, tmp_path):
    action, rows, original = excluded_request
    other = {**rows[0], 'request_id': 'another-request'}
    live_rows = [*rows, other]
    root = tmp_path / 'runtime/finance_preaudit_flows'
    calls = []
    def live_status():
        return {'credentials': {'status': 'ok'}, 'oa': {'execution_identity': 'verified',
            'by_workflow': {'travel_expense': len(live_rows)}}}
    def raw_reconciliation():
        return reconciliation.reconcile(root, pending_rows=live_rows, handled_rows=[])
    def pipeline(count, capacity):
        calls.append((count, capacity))
        live_rows[:] = rows
        return probe.PoolResult(requested=count, started=count, completed=count, failed=0, timed_out=0,
            stopped=0, peak_active=1, elapsed_seconds=1,
            outcomes=(probe.TaskOutcome(0, 'completed', {'status': 'completed', 'token_usage': {'status': 'unknown'}}),))
    result = probe.run_rolling_drain(capacity=2, live_status=live_status, reconcile_status=raw_reconciliation,
        pipeline_runner=pipeline, excluded_action_files=[action], exclusion_issue=710)
    assert calls == [(1, 2)]
    assert result['status'] == 'completed' and result['final_pending'] == 0
    assert result['execution_exclusions']['raw_continuation'] == original['continuation']
    assert result['execution_exclusions']['historical_submission_verified'] is False
    run = controller_handoff._read_manifest()['run_id']
    receipt = json.loads((approval_lifecycle.run_directory(run) / 'execution-exclusions.json').read_text())
    assert receipt['raw_continuation']['blocking_reasons'] == {'identity_conflict': 1}
    assert raw_reconciliation()['continuation']['status'] == 'blocked'
    assert len(list(action.parent.parent.glob('attempt-*'))) == 1


def test_rolling_excludes_both_pending_versions_without_dispatching(excluded_request, tmp_path):
    action, rows, _ = excluded_request
    versions = [*rows, {**rows[0], 'received_at': '2026-08-24T09:00:00+08:00'}]
    raw = reconciliation.reconcile(tmp_path / 'runtime/finance_preaudit_flows',
        pending_rows=versions, handled_rows=[])
    original = deepcopy(raw)
    original_files = {path: path.read_bytes() for path in action.parent.iterdir() if path.is_file()}
    scope = request_exclusions.Scope.bind(tmp_path, [action], issue=710)
    projected = scope.project(raw)
    assert projected['scope_excluded_pending'] == 2
    assert projected['quarantine']['actionable_pending'] == 0
    live = {'credentials': {'status': 'ok'}, 'oa': {'execution_identity': 'verified',
        'by_workflow': {'travel_expense': 2}}}
    assert probe._supported_pending(live) == 2
    result = probe.run_rolling_drain(capacity=2, live_status=lambda: live, reconcile_status=lambda: raw,
        pipeline_runner=lambda *_: pytest.fail('excluded versions dispatched an empty task'),
        excluded_action_files=[action], exclusion_issue=710)
    assert result['status'] == 'completed'
    assert result['initial_pending'] == result['initial_actionable'] == result['final_pending'] == 0
    assert result['waves'] == 0
    assert result['execution_exclusions']['raw_continuation'] == raw['continuation']
    assert raw == original and raw['continuation']['status'] == 'blocked'
    assert raw['continuation']['blocking_reasons'] == {'identity_conflict': 1}
    assert all(path.read_bytes() == data for path, data in original_files.items())
    assert len(list(action.parent.parent.glob('attempt-*'))) == 1


@pytest.mark.parametrize('extra_blocker', [False, True])
def test_exclusion_does_not_bypass_identity_or_other_controller_blocks(excluded_request, extra_blocker):
    action, _, raw = excluded_request
    current = deepcopy(raw)
    if extra_blocker:
        current['continuation']['blocking_reasons']['pending_write_state_unresolved'] = 1
    live = {'credentials': {'status': 'ok'}, 'oa': {'execution_identity': 'verified' if extra_blocker else 'unknown',
        'by_workflow': {'travel_expense': 1}}}
    with pytest.raises(ValueError, match='production_rolling_reconciliation_invalid|production_drain_live_status_invalid'):
        probe.run_rolling_drain(capacity=2, live_status=lambda: live, reconcile_status=lambda: current,
            pipeline_runner=lambda *_: pytest.fail('blocked pipeline ran'), excluded_action_files=[action], exclusion_issue=710)


@pytest.mark.parametrize('flags', [[], ['--rolling'], ['--rolling', '--exclusion-issue', '0'],
    ['--rolling', '--exclusion-issue', '-1']])
def test_cli_rejects_exclusions_without_explicit_rolling_and_positive_issue(monkeypatch, capsys, flags):
    monkeypatch.setattr(probe, 'run_rolling_drain', lambda **_: pytest.fail('invalid exclusion reached controller'))
    assert probe.main(['--confirm-write', 'YES', '--exclude-action-file', '/offline/action.json', *flags]) == 1
    assert 'exclusion' in json.loads(capsys.readouterr().out)['error_code']


def test_cli_routes_repeated_explicit_exclusions_without_changing_defaults(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(probe, 'run_rolling_drain', lambda **kwargs: calls.append(kwargs) or {'status': 'completed'})
    assert probe.main(['--rolling', '--confirm-write', 'YES', '--exclude-action-file', '/offline/one/action.json',
        '--exclude-action-file', '/offline/two/action.json', '--exclusion-issue', '710']) == 0
    assert calls[-1]['excluded_action_files'] == (Path('/offline/one/action.json'), Path('/offline/two/action.json'))
    assert calls[-1]['exclusion_issue'] == 710
    assert probe.main(['--rolling', '--confirm-write', 'YES']) == 0
    assert 'excluded_action_files' not in calls[-1] and 'exclusion_issue' not in calls[-1]
    capsys.readouterr()


@pytest.mark.parametrize('stage', ['admission', 'preparation', 'recovery'])
def test_excluded_collecting_claim_keeps_current_pending_admission(tmp_path, monkeypatch, stage):
    excluded, rows, _ = _collecting_claim(tmp_path)
    root = tmp_path / 'runtime/finance_preaudit_flows'

    other = _candidates(('another-request',))
    engine = _engine(root, candidates=other, collect_material=_downloaded_material,
                     authorization_source=_grant, submit_frozen=_success)
    action = engine.next_action('travel_expense', mode='controlled_write', current_pending=True)
    assert action['action'] == 'review_required'
    Path(action['action_file']).parent.joinpath('attachments/evidence.bin').unlink()
    original = {path: path.read_bytes() for path in root.rglob('*.json')}
    live_rows = [*rows, *other]
    monkeypatch.setattr(probe, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(runtime_control, 'PROJECT_ROOT', tmp_path)
    if stage == 'preparation':
        monkeypatch.setattr(probe.cli, '_live_pending_identities', lambda: pending(live_rows))
        monkeypatch.setattr(probe.cli, '_live_handled_identities', lambda: [])
        monkeypatch.setattr(probe.cli, '_live_flow_engine', lambda **_: engine)
        monkeypatch.setattr(probe, '_historical_write_authorization_failures', frozenset)
        result = probe._prepare_one(selection(tmp_path, excluded))
        assert result['action'] == 'review_required', result
        assert Path(result['action_file']).parent.name == 'attempt-2'
        assert all(path.read_bytes() == raw for path, raw in original.items())
        return
    modes = []

    def reconcile(workflows=None, *, current_pending=True):
        modes.append(current_pending)
        return reconciliation.reconcile(root, pending_rows=live_rows, handled_rows=[],
                                        current_pending=current_pending)

    def live():
        return {'credentials': {'status': 'ok'}, 'oa': {'execution_identity': 'verified',
                'by_workflow': {'travel_expense': len(live_rows)}}}

    calls = []

    def pipeline(count, capacity, **_kwargs):
        calls.append((count, capacity))
        live_rows[:] = rows
        return probe.PoolResult(requested=count, started=count, completed=count, failed=0,
            timed_out=0, stopped=0, peak_active=1, elapsed_seconds=1,
            outcomes=(probe.TaskOutcome(0, 'completed', {'status': 'completed',
                                                       'token_usage': {'status': 'unknown'}}),))

    monkeypatch.setattr(probe, '_production_reconciliation', reconcile)
    if stage == 'recovery':
        from apps.oa_finance_preaudit import production_write
        monkeypatch.setattr(probe, '_run_global_dispatched_tasks', pipeline)
        monkeypatch.setattr(approval_lifecycle, 'startup', lambda *_args, **_kwargs: {'status': 'ready'})
        monkeypatch.setattr(production_write, '_authorized_workflows', lambda *_: probe.ROLLING_WORKFLOW_KEYS)
        with pytest.raises(ValueError, match='production_rolling_reconciliation_invalid'):
            probe.run_rolling_drain(capacity=3, live_status=live, reconcile_status=reconcile,
                pipeline_runner=pipeline, excluded_action_files=[excluded], exclusion_issue=731,
                authorized_recovery_once=True)
        assert modes and not any(modes) and calls == []
        assert all(path.read_bytes() == raw for path, raw in original.items())
        return
    result = probe.run_rolling_drain(capacity=3, live_status=live, reconcile_status=reconcile,
        pipeline_runner=pipeline, excluded_action_files=[excluded], exclusion_issue=731)
    assert result['status'] == 'completed' and calls == [(1, 3)]
    assert modes and all(modes)
    assert result['execution_exclusions']['raw_continuation']['status'] == 'ready'
    assert all(path.read_bytes() == raw for path, raw in original.items())
