"""One-run queue scoping must never mutate or recover the deferred review."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json

import pytest

from apps.oa_finance_preaudit import flow_engine, reconciliation, production_write, review_interruption
from scripts import finance_preaudit_concurrency_probe as probe
from tests.finance_preaudit.test_flow_engine_support import _grant
from tests.finance_preaudit.test_flow_engine import _travel_material
from tests.finance_preaudit.test_concurrency_probe import isolate_alert_delivery  # noqa: F401
from tests.finance_preaudit.review_profiles import retained_deepseek_cli  # noqa: F401


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(probe, 'PROJECT_ROOT', root)
    matches = probe._prepared_action_matches_selection
    monkeypatch.setattr(probe, '_prepared_action_matches_selection',
        lambda *args, **kwargs: matches(*args, **{**kwargs, 'project_root': root}))
    flows = root / 'runtime' / 'finance_preaudit_flows'
    rows = [
        {'request_id': 'deferred', 'received_at': '2026-09-01 09:00:00', 'workflow_key': 'travel_expense'},
        {'request_id': 'next-travel', 'received_at': '2026-09-01 09:01:00', 'workflow_key': 'travel_expense'},
        {'request_id': 'next-support', 'received_at': '2026-09-01 09:02:00', 'workflow_key': 'business_support_fee'},
    ]
    selected = []
    collected = []

    def engine(identity):
        selected.append(identity)
        def collect(request, attempt):
            collected.append(request)
            workflow = next(row['workflow_key'] for row in rows if row['request_id'] == request)
            material = _travel_material(request)
            material['workflow_key'] = workflow
            if workflow == 'business_support_fee':
                material['workflow_name'] = '业务支持费报销流程'
            return material
        return flow_engine.FlowEngine(root=flows,
            discover_candidates=lambda workflow: [row for row in rows if row['workflow_key'] == workflow
                and (row['request_id'], row['received_at']) == identity],
            discover_handled=lambda: [], collect_material=collect,
            authorization_source=_grant, authorization_verifier=lambda grant, context: grant == _grant(context),
            submit_frozen=lambda _: pytest.fail('No real or simulated submission is needed to defer a review'))

    first = engine(('deferred', rows[0]['received_at'])).next_action('travel_expense', mode='controlled_write')
    path = Path(first['action_file'])
    assert first['write_state'] == 'authorized', first.get('reason')
    monkeypatch.setattr(probe.cli, '_live_flow_engine', lambda *, controlled, selected_identity: engine(selected_identity))
    monkeypatch.setattr(probe.cli, '_live_pending_identities', lambda: [
        {'requestId': row['request_id'], 'received_at': row['received_at'], 'flow_key': row['workflow_key'],
         'support_status': 'supported', 'can_submit': True} for row in rows])
    def report():
        return reconciliation.reconcile(flows, pending_rows=rows, handled_rows=[])
    def live():
        return {'credentials': {'status': 'ok'}, 'oa': {'execution_identity': 'verified',
            'by_workflow': dict(Counter(row['workflow_key'] for row in rows))}}
    return path, rows, report, live, selected, collected


def selection(paths=(), assigned=()):
    return {'workflow_key': '*', 'assigned_identities': list(assigned),
        'historical_unknown_identities': [], 'defer_action_files': [str(path) for path in paths]}


def test_prepare_excludes_exact_review_and_next_round_admits_it(recorded):
    path, rows, _report, _live, selected, collected = recorded
    # Existing read-only leases can update lock files; evidence and state must stay unchanged.
    before = {file.name: file.read_bytes() for file in path.parent.iterdir()
              if file.is_file() and file.suffix != '.lock'}
    result = probe._prepare_one(selection([path]))
    assert result.get('action') == 'review_required', result
    assert selected[-1][0] == 'next-travel'
    result = probe._prepare_one(selection([path], [[rows[1]['request_id'], rows[1]['received_at']]]))
    assert result.get('action') == 'review_required', result
    assert selected[-1][0] == 'next-support'
    after = {file.name: file.read_bytes() for file in path.parent.iterdir()
             if file.is_file() and file.suffix != '.lock'}
    assert before == after, [name for name in before.keys() | after.keys() if before.get(name) != after.get(name)]
    assert probe._prepare_one(selection())['action_file'] == str(path)
    assert collected.count('deferred') == 1


@pytest.mark.parametrize('blocker', ['identity_conflict', 'pending_write_state_unresolved'])
def test_snapshot_keeps_original_counts_and_reconciliation_gates(recorded, blocker):
    path, _rows, report, live, *_ = recorded
    assert probe._rolling_snapshot(live, report, deferred_action_files=[path]) == (3, 0, 2, 0)
    blocked = report()
    blocked['continuation'].update(status='blocked', scope='controller', blocking_reasons={blocker: 1})
    with pytest.raises(ValueError, match='production_rolling_reconciliation_invalid'):
        probe._rolling_snapshot(live, lambda: blocked, deferred_action_files=[path])


@pytest.mark.parametrize('bad', ['missing', 'not_object', 'duplicate'])
def test_snapshot_rejects_untrusted_deferred_count(recorded, bad):
    path, _rows, report, live, *_ = recorded
    payload = report()
    if bad == 'missing':
        del payload['findings']
    elif bad == 'not_object':
        payload['findings'].append(None)
    else:
        matching = next(row for row in payload['findings']
                        if row['item_key'] == path.parent.parent.name)
        payload['findings'].append(dict(matching))
    with pytest.raises(ValueError, match='production_rolling_reconciliation_invalid'):
        probe._rolling_snapshot(live, lambda: payload, deferred_action_files=[path])


def test_only_deferred_review_finishes_without_counting_a_submission(recorded):
    path, rows, report, live, *_ = recorded
    rows[:] = rows[:1]
    result = probe.run_rolling_drain(capacity=3, live_status=live, reconcile_status=report,
        deferred_action_files=[path], pipeline_runner=lambda *_: pytest.fail('Deferred review reached dispatcher'))
    assert result['status'] == 'completed'
    assert result['completed'] == result['waves'] == result['quarantined_pending'] == 0
    assert result['final_pending'] == result['deferred_selected_count'] == 1
    assert result['empty_snapshots'] == 2
    assert result['deferred_action_files'] == [str(path)]


@pytest.mark.parametrize('artifact', ['writer-boundary.json', 'execution-receipt.json',
    'review-decision.json', 'remark.txt', 'submission-envelope.json'])
def test_rejects_any_canonical_write_product(recorded, artifact):
    path, *_ = recorded
    path.with_name(artifact).write_text('{}')
    with pytest.raises(ValueError, match='production_rolling_defer_invalid'):
        probe._deferred_action_states([path], probe.ROLLING_WORKFLOW_KEYS)


@pytest.mark.parametrize('state', ['unknown', 'submitting', 'submitted', 'not_requested'])
def test_rejects_non_authorized_states(recorded, state):
    path, *_ = recorded
    target = path.with_name('state.json')
    payload = json.loads(target.read_text())
    payload['write_state'] = state
    target.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='production_rolling_defer_invalid'):
        probe._deferred_action_states([path], probe.ROLLING_WORKFLOW_KEYS)


@pytest.mark.parametrize('bad', ['freeze', 'duplicate', 'outside', 'symlink', 'old_attempt', 'scope'])
def test_rejects_untrusted_or_ambiguous_selection(recorded, tmp_path, bad):
    path, *_ = recorded
    paths = [path]
    scope = probe.ROLLING_WORKFLOW_KEYS
    if bad == 'freeze':
        path.with_name('material.json').write_text('{}')
    elif bad == 'duplicate':
        paths.append(path)
    elif bad == 'outside':
        paths = [tmp_path / 'action.json']
    elif bad == 'symlink':
        link = tmp_path / 'alias'
        link.symlink_to(path.parent, target_is_directory=True)
        paths = [link / 'action.json']
    elif bad == 'old_attempt':
        target = path.parent.parent / 'attempt-2'
        target.mkdir()
        state = json.loads(path.with_name('state.json').read_text())
        state.update(attempt=2, action_file=str(target / 'action.json'), state_file=str(target / 'state.json'))
        (target / 'state.json').write_text(json.dumps(state))
    elif bad == 'scope':
        scope = frozenset({'general_expense'})
    with pytest.raises(ValueError, match='production_rolling_defer_invalid'):
        probe._deferred_action_states(paths, scope)


def test_cli_passes_repeatable_selection_and_rejects_read_only(monkeypatch, capsys):
    received = []
    monkeypatch.setattr(probe, 'run_rolling_drain', lambda **kwargs: received.append(kwargs) or {'status': 'completed'})
    assert probe.main(['--confirm-write', 'YES', '--defer-action-file', '/one/action.json',
        '--defer-action-file', '/two/action.json']) == 0
    assert received[0]['deferred_action_files'] == (Path('/one/action.json'), Path('/two/action.json'))
    assert probe.main(['--read-only', '--defer-action-file', '/one/action.json']) == 1
    assert len(received) == 1
    capsys.readouterr()


def test_explicit_recovery_overlap_is_rejected_before_begin(recorded, monkeypatch):
    path, _rows, report, live, *_ = recorded
    original = probe._run_global_dispatched_tasks
    monkeypatch.setattr(probe.approval_lifecycle, 'startup', lambda *a, **kw: {'status': 'ready'})
    monkeypatch.setattr(probe.controller_handoff, 'begin', lambda _: pytest.fail('Overlapping selection started'))
    with pytest.raises(ValueError, match='production_rolling_defer_recovery_overlap'):
        probe.run_rolling_drain(capacity=3, pipeline_runner=original, live_status=live,
            reconcile_status=report, recovery_action_files=[path], deferred_action_files=[path])


def test_automatic_recovery_overlap_is_rejected_before_begin(recorded, monkeypatch):
    path, _rows, report, live, *_ = recorded
    original_report = report()
    latest = reconciliation.latest_states
    monkeypatch.setattr(reconciliation, 'latest_states',
        lambda root: [*latest(root), {'reason': review_interruption.REASON}])
    monkeypatch.setattr(production_write, '_authorized_workflows', lambda _: probe.ROLLING_WORKFLOW_KEYS)
    monkeypatch.setattr(review_interruption, 'select', lambda *_: (path,))
    monkeypatch.setattr(probe.approval_lifecycle, 'startup', lambda *a, **kw: {'status': 'ready'})
    monkeypatch.setattr(probe.controller_handoff, 'begin', lambda _: pytest.fail('Overlapping selection started'))
    with pytest.raises(ValueError, match='production_rolling_defer_recovery_overlap'):
        probe.run_rolling_drain(capacity=3, pipeline_runner=probe._run_global_dispatched_tasks,
            live_status=live, reconcile_status=lambda: original_report, deferred_action_files=[path])


def test_dispatcher_keeps_fifo_for_other_reviews_and_never_writes_deferred(recorded, monkeypatch):
    path, rows, _report, _live, *_ = recorded
    before = {file.name: file.read_bytes() for file in path.parent.iterdir()
              if file.is_file() and file.suffix != '.lock'}
    written = []
    monkeypatch.setattr(probe, '_preparation_executor', lambda: ThreadPoolExecutor(max_workers=1))

    def review(task):
        return {'status': 'reviewed', 'business_state': 'not_available', 'write_state': 'not_requested',
            'process_attempts': 1, 'process_retries': 0, 'token_usage': {'status': 'unknown'},
            'decision': {'kind': 'preaudit_review_decision', 'overall': 'needs_human'}}

    def complete(action, reviewed):
        state = json.loads(action.with_name('state.json').read_text())
        written.append(state['request_id'])
        return {**reviewed, 'status': 'completed', 'business_state': 'needs_human',
            'write_state': 'submitted', 'error_code': None}

    result = probe._run_dispatched_tasks(2, 2, project_root=path.parents[6],
        strict_global_fifo=True, deferred_action_files=[path],
        prepare_action=probe._prepare_one, review_worker=review, complete_review=complete,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {'status': 'healthy'})
    assert result.completed == 2
    assert written == [row['request_id'] for row in rows[1:]]
    assert before == {file.name: file.read_bytes() for file in path.parent.iterdir()
                      if file.is_file() and file.suffix != '.lock'}
