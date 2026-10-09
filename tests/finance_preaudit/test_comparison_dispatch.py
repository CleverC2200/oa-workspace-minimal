"""Actual source-host transport with a local CLI, never financial acceptance."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import pytest
import subprocess
import sys

from apps.oa_finance_preaudit import review_experiment as e
from tests.finance_preaudit.test_review_experiment import plan, sources  # noqa: F401
from tests.finance_preaudit.test_candidate_phase import ref
from tests.finance_preaudit.test_candidate_phase import phase, phase_source  # noqa: F401
from tests.finance_preaudit import test_review_experiment as fixtures


def test_only_selected_pending_side_consumes_local_cli_and_preview(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit.review_comparison import _execute_pending_pair

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    path, _auth, before, after = plan(tmp_path, sources, purpose='diagnostic')
    e._save(path.with_name(path.name + '.started.json'), {'plan_sha256': ref(path)['sha256']})
    original = e._host(path, 'baseline', execute=True)
    old_result = (before / 'result.json').read_bytes()
    selection = {'target_plan_ref': ref(path), 'reused_originals': {'baseline': {'new_starts': 0}},
                 'pending_arms': ['candidate']}
    authority = {'not_before': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                 'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    result = _execute_pending_pair(path, selection, authority, revalidate=lambda: None)

    assert original['cli_starts'] == 1
    assert result['status'] == 'awaiting_independent_assessment', result
    assert result['new_cli_starts'] == 1 and result['dispatched_arms'] == ['candidate']
    assert result['reports']['candidate']['acceptance']['preview'] == 'verified_closed'
    assert result['reports']['candidate']['usage']['total_tokens'] == 110
    assert (before / 'result.json').read_bytes() == old_result
    assert json.loads((after / 'result.json').read_text())['write_state'] == 'not_requested'


def test_first_output_rejection_charges_attempt_and_stops_other_side(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit.review_comparison import _execute_pending_pair

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    path, _auth, before, after = plan(tmp_path, sources, purpose='diagnostic', final='not JSON')
    selection = {'target_plan_ref': ref(path), 'reused_originals': {},
                 'pending_arms': ['baseline', 'candidate']}
    result = _execute_pending_pair(path, selection, {}, revalidate=lambda: None)

    assert result['status'] == 'stopped' and result['new_cli_starts'] == 1, result
    assert result['reason'] == 'comparison_output_rejected'
    assert result['reports']['baseline']['usage']['total_tokens'] == 110
    assert (before / 'reviewer/decision.json').read_text() == 'not JSON'
    assert result['not_started'] == ['candidate']
    assert not (after / 'started.json').exists()


def test_expiry_between_actual_sides_preserves_first_cost(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit.review_comparison import _execute_pending_pair

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    path, _auth, before, after = plan(tmp_path, sources, purpose='diagnostic')
    selection = {'target_plan_ref': ref(path), 'reused_originals': {},
                 'pending_arms': ['baseline', 'candidate']}

    def authority_changed():
        if (before / 'result.json').exists():
            raise ValueError('authorization_expired')

    result = _execute_pending_pair(path, selection, {}, revalidate=authority_changed)
    assert result['status'] == 'stopped' and result['new_cli_starts'] == 1
    assert result['reason'] == 'authorization_expired'
    assert result['reports']['baseline']['usage']['total_tokens'] == 110
    assert not (after / 'started.json').exists()


def test_missing_start_receipt_after_actual_cli_keeps_unknown_and_raw_cost(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit.review_comparison import _execute_pending_pair

    original_cli = fixtures.fake_cli

    def broken_cli(*args, **kwargs):
        cli = original_cli(*args, **kwargs)
        cli.write_text(cli.read_text() + "\nPath(root, 'cli-start.json').unlink()\n")
        return cli

    monkeypatch.setattr(fixtures, 'fake_cli', broken_cli)
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    path, _auth, before, after = plan(tmp_path, sources, purpose='diagnostic')
    selection = {'target_plan_ref': ref(path), 'reused_originals': {},
                 'pending_arms': ['baseline', 'candidate']}
    result = _execute_pending_pair(path, selection, {}, revalidate=lambda: None)

    assert result['status'] == 'stopped' and result['new_cli_starts'] == 'unknown', result
    assert result['raw_results']['baseline']['execution']['token_usage']['total_tokens'] == 110
    assert (before / 'result.json').exists()
    assert not (after / 'started.json').exists()


def test_archived_baseline_without_native_contract_is_explicitly_unsupported(tmp_path, sources):
    from apps.oa_finance_preaudit.review_comparison import _ComparisonCaller

    path, _auth, before, after = plan(tmp_path, sources, purpose='diagnostic')
    adapter = tmp_path / 'neutral-adapter.py'
    adapter.write_text('"""No financial qualification or dispatch in this fixture."""\n')
    adapter.chmod(0o600)
    proof = tmp_path / 'neutral-proof.json'
    proof.write_text('{}')
    proof.chmod(0o600)
    caller = _ComparisonCaller(caller_file=Path(__file__), identity_bank={}, adapter_file=ref(adapter))

    with pytest.raises(ValueError, match='source_native_contract_unavailable_baseline'):
        caller.native_ready({'plan': ref(path)}, {'native_contract': ref(proof)},
                            {'pending_arms': ['baseline', 'candidate']})

    assert not (before / 'started.json').exists()
    assert not (after / 'started.json').exists()


def test_actual_cancelled_local_cli_keeps_cost_and_reaps_before_next_side(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit.review_comparison import _execute_pending_pair

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    path, _auth, before, after = plan(tmp_path, sources, purpose='diagnostic', final='cancel')
    selection = {'target_plan_ref': ref(path), 'reused_originals': {},
                 'pending_arms': ['baseline', 'candidate']}
    result = _execute_pending_pair(path, selection, {}, revalidate=lambda: None)

    assert result['status'] == 'stopped' and result['new_cli_starts'] == 1, result
    assert result['reason'] == 'cancelled_during_review'
    assert result['raw_results']['baseline']['execution']['process_state'] == 'cancelled'
    assert result['raw_results']['baseline']['execution']['token_usage']['total_tokens'] == 110
    assert not (after / 'started.json').exists()
    import os
    pid = json.loads((before / 'cli-start.json').read_text())['pid']
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize('record_state', ['complete', 'missing_result', 'reused_reference'])
def test_current_final_phase_counts_both_sides_without_relabeling_diagnostics(tmp_path, sources, monkeypatch, record_state):
    from apps.oa_finance_preaudit.review_comparison import _execute_pending_pair, _parent_budget
    from tests.finance_preaudit.test_review_experiment import save

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    directory = tmp_path / 'pair'
    directory.mkdir(mode=0o700)
    path, _auth, _before, _after = plan(directory, sources, purpose='diagnostic')
    parent = tmp_path / 'parent.json'
    save(parent, {'includes_previous_starts': 0})
    origin_scope, origin_authority = tmp_path / 'origin-scope.json', tmp_path / 'origin-authority.json'
    save(origin_scope, {'cases': []})
    save(origin_authority, {'scope_sha256': ref(origin_scope)['sha256'],
                            'parent_cumulative_authority': ref(parent), 'prior_plans': []})
    e._bind_candidate_phase_parent(ref(parent), ref(origin_scope), ref(origin_authority))
    scope_file = tmp_path / 'scope.json'
    save(scope_file, {'cases': [{'alias': 'synthetic-pair', 'plan': ref(path)}]})
    scope = json.loads(scope_file.read_text())
    authority = {'scope_sha256': ref(scope_file)['sha256'], 'parent_cumulative_authority': ref(parent),
                 'prior_phase_scope': ref(origin_scope), 'prior_phase_authority': ref(origin_authority),
                 'prior_plans': [], 'phase': 'final_production_code_comparison'}
    counts = _parent_budget(authority, scope, ref(scope_file))
    assert counts['global_cli_starts'] == counts['diagnostic_cli_starts'] == 0

    result = _execute_pending_pair(path, {'target_plan_ref': ref(path), 'reused_originals': {},
                                         'pending_arms': ['baseline', 'candidate']}, {}, revalidate=lambda: None)
    assert result['new_cli_starts'] == 2
    if record_state == 'missing_result':
        completed = Path(json.loads(path.read_text())['arms']['baseline'])
        (completed / 'result.json').unlink()
        with pytest.raises((ValueError, OSError)):
            _parent_budget(authority, scope, ref(scope_file))
        return
    if record_state == 'reused_reference':
        # A later plan may point to the exact same consumed original archive.
        # Reference reuse never becomes a second start in that later phase.
        new_plan = tmp_path / 'reference-plan.json'
        new_plan.write_bytes(path.read_bytes())
        new_plan.chmod(0o600)
        first_authority = tmp_path / 'first-final-authority.json'
        save(first_authority, authority)
        e._claim_candidate_phase(scope_file, first_authority, authority)
        original_scope = ref(scope_file)
        scope_file = tmp_path / 'later-scope.json'
        scope['cases'][0].update(plan=ref(new_plan), reuse={
            arm: {'plan': ref(path), 'arm': arm} for arm in ('baseline', 'candidate')})
        save(scope_file, scope)
        authority['scope_sha256'] = ref(scope_file)['sha256']
        authority.update(prior_phase_scope=original_scope,
                         prior_phase_authority=ref(first_authority), prior_plans=[ref(path)])
        counts = _parent_budget(authority, scope, ref(scope_file))
        assert counts['phase_cli_starts'] == 0 and counts['global_cli_starts'] == 2
        assert counts['diagnostic_cli_starts'] == 0
        return
    counts = _parent_budget(authority, scope, ref(scope_file))
    assert counts['global_cli_starts'] == counts['phase_cli_starts'] == 2
    assert counts['diagnostic_cli_starts'] == 0


def test_next_pair_waits_for_independent_quality_after_actual_preview(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit.review_comparison import _ComparisonCaller, _execute_pending_pair

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    directory = tmp_path / 'first'
    directory.mkdir(mode=0o700)
    path, _auth, _before, _after = plan(directory, sources, purpose='diagnostic')
    result = _execute_pending_pair(path, {'target_plan_ref': ref(path), 'reused_originals': {},
                                        'pending_arms': ['baseline', 'candidate']}, {}, revalidate=lambda: None)
    assert result['new_cli_starts'] == 2
    assert result['status'] == 'awaiting_independent_assessment'
    assert all(result['reports'][arm]['acceptance']['preview'] == 'verified_closed'
               for arm in ('baseline', 'candidate'))
    adapter = tmp_path / 'neutral-adapter.py'
    adapter.write_text('"""No financial qualification or dispatch in this fixture."""\n')
    adapter.chmod(0o600)
    caller = _ComparisonCaller(caller_file=Path(__file__), identity_bank={}, adapter_file=ref(adapter))
    target = {'alias': 'second', 'plan': ref(path)}
    scope = {'cases': [{'alias': 'first', 'plan': ref(path)}, target]}

    with pytest.raises(ValueError, match='^previous_baseline_quality_pending$'):
        caller._previous_quality(scope, target)


@pytest.mark.parametrize('role, outcome, expected, unique', [('engineering_draft', 'pass', 'unverified', None),
                                                    ('historical_crosscheck', 'pass', 'pass', None),
                                                    ('historical_crosscheck', 'fail', 'fail', None),
                                                    pytest.param('historical_crosscheck', 'unverified', 'unverified', False,
                                                                 id='business_scope_unknown'),
                                                    pytest.param('historical_crosscheck', 'unverified', 'unverified', True,
                                                                 id='business_scope_unique_unknown_stops'),
                                                    pytest.param('historical_crosscheck', 'unverified', 'unverified', None,
                                                                 id='business_scope_unmarked_unknown_stops'),
                                                    pytest.param('historical_crosscheck', 'unverified', 'unverified', 'late',
                                                                 id='business_scope_late_unknown_stops'),
                                                    pytest.param('engineering_draft', 'unverified', 'unverified', False,
                                                                 id='business_scope_missing_independent_audit_stops')])
def test_baseline_next_pair_requires_complete_independent_audit(tmp_path, phase_source, monkeypatch, role, outcome, expected, unique):
    from apps.oa_finance_preaudit import flow_evals, review_batch_report as batch
    from apps.oa_finance_preaudit.review_comparison import _ComparisonCaller
    from tests.finance_preaudit.test_review_experiment import save

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    _scope, _authority, rows = phase(tmp_path, phase_source, count=1)
    row = rows[0]
    path = Path(row['plan']['file'])
    plan_value = json.loads(path.read_text())
    baseline_checklist, baseline_expectations = row['checklist'], row['expectations']
    if unique is not None:
        original = json.loads(Path(row['checklist']['file']).read_text())
        original['business_scope_is_unique'] = False if unique == 'late' else unique
        name = path.parent / 'baseline-checklist.json'
        save(name, original)
        baseline_checklist = ref(name)
        baseline_expectations = flow_evals.freeze_financial_expectations(
            baseline_checklist, path.parent / 'baseline-expectations.json')
    e._save(path.with_name(path.name + '.started.json'), {'plan_sha256': ref(path)['sha256']})
    e._host(path, 'baseline', execute=True)
    if unique == 'late':
        baseline_expectations = flow_evals.freeze_financial_expectations(
            baseline_checklist, path.parent / 'late-baseline-expectations.json')
    root = Path(plan_value['arms']['baseline'])
    report = batch.build_report(root)
    action = Path(json.loads((root / 'manifest.json').read_text())['action_file'])
    checklist = json.loads(Path(baseline_checklist['file']).read_text())
    assessment = path.parent / 'baseline-assessment.json'
    save(assessment, {'schema_version': 1, 'method': 'independent_manual',
                     'reviewer_role': role, 'reviewer_record': 'controlled-only',
                     'reviewed_at': datetime.now(timezone.utc).isoformat(),
                     'item_key': report['financial_binding']['item_key'],
                     'archive_manifest_sha256': ref(root / 'manifest.json')['sha256'],
                     'result_sha256': ref(root / 'result.json')['sha256'],
                     'checklist_sha256': baseline_checklist['sha256'],
                     'output_refs': [ref(p) for p in [root / 'result.json', root / 'reviewer/decision.json',
                                     action.with_name('review-decision.json'), action.with_name('remark.txt'),
                                     action.with_name('execution-receipt.json')]],
                     'checks': [{'id': check['id'], 'outcome': outcome,
                                 'reason': 'Controlled independent original/consumed comparison.'}
                                for check in checklist['checks']]})
    quality = path.parent / 'baseline-quality.json'
    save(quality, flow_evals.build_financial_quality_report([{
        'item_key': report['financial_binding']['item_key'], 'archive': ref(root / 'manifest.json'),
        'checklist': baseline_checklist, 'assessment': ref(assessment), 'expectations': baseline_expectations}]))
    save(path.parent / 'baseline-audit-binding.private.json', {
        'quality_report': ref(quality), 'assessment': ref(assessment),
        'checklist': baseline_checklist, 'expectations': baseline_expectations})
    row.update(baseline_checklist=baseline_checklist, baseline_expectations=baseline_expectations)

    actual = _ComparisonCaller._baseline_quality(row, path, report)

    assert actual['status'] == expected
    assert actual.get('audit_complete') is (role == 'historical_crosscheck')
    if unique is False and role == 'historical_crosscheck':
        assert actual['comparison_audit'] == 'verified_business_scope_unknown'
        assert actual['status'] == 'unverified'
        assert json.loads(quality.read_text())['eligible'] is False
    # An independently confirmed production defect stays in the comparison.
    # Candidate quality still must pass before the next pair can advance.
    e._host(path, 'candidate', execute=True)
    from tests.finance_preaudit.test_candidate_phase import audit
    audit(row)
    adapter = tmp_path / 'neutral-adapter.py'
    adapter.write_text('"""No reference selection in this neutral fixture."""\n')
    adapter.chmod(0o600)
    caller = _ComparisonCaller(caller_file=Path(__file__), identity_bank={}, adapter_file=ref(adapter))
    target = {'alias': 'second', 'plan': row['plan']}
    scope = {'cases': [row, target]}
    if role == 'engineering_draft' or outcome == 'unverified' and unique is not False:
        with pytest.raises(ValueError, match='previous_baseline_quality_unverified'):
            caller._previous_quality(scope, target)
    else:
        caller._previous_quality(scope, target)
    if unique is False and role == 'historical_crosscheck':
        assessment_file = path.parent / 'assessment.json'
        candidate_assessment = json.loads(assessment_file.read_text())
        for check in candidate_assessment['checks']:
            check['outcome'] = 'unverified'
        unknown_assessment = path.parent / 'candidate-unknown-assessment.json'
        save(unknown_assessment, candidate_assessment)
        candidate_report = batch.build_report(Path(plan_value['arms']['candidate']))
        unknown_quality = path.parent / 'candidate-unknown-quality.json'
        save(unknown_quality, flow_evals.build_financial_quality_report([{
            'item_key': candidate_report['financial_binding']['item_key'],
            'archive': ref(Path(plan_value['arms']['candidate']) / 'manifest.json'),
            'checklist': row['checklist'], 'assessment': ref(unknown_assessment), 'expectations': row['expectations']}]))
        save(path.parent / 'candidate-audit-binding.private.json', {
            'quality_report': ref(unknown_quality), 'assessment': ref(unknown_assessment), 'checklist': row['checklist']})
        with pytest.raises(ValueError, match='previous_quality_unverified'):
            caller._previous_quality(scope, target)


def test_explicit_baseline_scope_unknown_cannot_hide_unobserved_original_image(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit import flow_evals, review_batch_report as batch
    from apps.oa_finance_preaudit.review_comparison import _ComparisonCaller
    from tests.finance_preaudit.test_review_experiment import save

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    path, _authority, before, _after = plan(tmp_path, sources, purpose='diagnostic', image=True)
    manifest = json.loads((before / 'manifest.json').read_text())
    action = Path(manifest['action_file'])
    packet = json.loads(action.with_name('review-packet.json').read_text())
    names = ('SKILL.md', 'references/result-contract.md', 'references/observation-contract.md',
             'references/flow-engine-contract.md', 'references/flows/business-entertainment.md')
    checklist = tmp_path / 'baseline-checklist.json'
    save(checklist, {'schema_version': 1, 'workflow_key': 'business_entertainment',
                    'source_kind': 'synthetic', 'scenario': 'ordinary_payment',
                    'review_scope': 'complete_financial_review', 'business_scope_is_unique': False,
                    'source_refs': [ref(Path(p)) for p in manifest['originals']],
                    'rule_refs': [ref(before / 'calling-source/.agents/skills/oa-finance-preaudit' / name)
                                  for name in names],
                    'checks': [{'id': item['item_key'], 'item_key': item['item_key'],
                                'expected': 'Controlled business scope is explicitly not unique.'}
                               for item in packet['items']]})
    expectations = flow_evals.freeze_financial_expectations(ref(checklist), tmp_path / 'baseline-expectations.json')
    e._save(path.with_name(path.name + '.started.json'), {'plan_sha256': ref(path)['sha256']})
    e._host(path, 'baseline', execute=True)
    report = batch.build_report(before)
    assert report['acceptance']['original_images']['status'] == 'incomplete'
    assessment = tmp_path / 'baseline-assessment.json'
    save(assessment, {'schema_version': 1, 'method': 'independent_manual',
                     'reviewer_role': 'historical_crosscheck', 'reviewer_record': 'controlled-only',
                     'reviewed_at': datetime.now(timezone.utc).isoformat(),
                     'item_key': report['financial_binding']['item_key'],
                     'archive_manifest_sha256': ref(before / 'manifest.json')['sha256'],
                     'result_sha256': ref(before / 'result.json')['sha256'], 'checklist_sha256': ref(checklist)['sha256'],
                     'output_refs': [ref(p) for p in [before / 'result.json', before / 'reviewer/decision.json',
                                     action.with_name('review-decision.json'), action.with_name('remark.txt'),
                                     action.with_name('execution-receipt.json')]],
                     'checks': [{'id': item['item_key'], 'outcome': 'unverified',
                                 'reason': 'Controlled old business scope has no unique result.'}
                                for item in packet['items']]})
    quality = tmp_path / 'baseline-quality.json'
    save(quality, flow_evals.build_financial_quality_report([{
        'item_key': report['financial_binding']['item_key'], 'archive': ref(before / 'manifest.json'),
        'checklist': ref(checklist), 'assessment': ref(assessment), 'expectations': expectations}]))
    save(path.parent / 'baseline-audit-binding.private.json', {
        'quality_report': ref(quality), 'assessment': ref(assessment), 'checklist': ref(checklist),
        'expectations': expectations})
    row = {'alias': 'first', 'plan': ref(path), 'baseline_checklist': ref(checklist),
           'baseline_expectations': expectations}
    target = {'alias': 'second', 'plan': ref(path)}
    adapter = tmp_path / 'neutral-adapter.py'
    adapter.write_text('"""No reference selection or financial qualification."""\n')
    adapter.chmod(0o600)
    caller = _ComparisonCaller(caller_file=Path(__file__), identity_bank={}, adapter_file=ref(adapter))

    with pytest.raises(ValueError, match='previous_baseline_images_unverified'):
        caller._previous_quality({'cases': [row, target]}, target)

    assert json.loads(quality.read_text())['cases'][0]['verdict'] == 'unverified'
    assert json.loads(quality.read_text())['eligible'] is False


@pytest.mark.parametrize('fault, reason', [
    ('missing_baseline', "'baseline_checklist'"),
    ('candidate_rule', 'final_expectations_source_mismatch'),
    ('late_freeze', 'final_expectations_not_frozen_before_dispatch')])
def test_final_scope_rejects_missing_or_drifted_pre_run_expectations(tmp_path, phase_source, monkeypatch, fault, reason):
    from apps.oa_finance_preaudit.review_comparison import _ComparisonCaller
    from tests.finance_preaudit.test_review_experiment import ROOT, save
    from tests.finance_preaudit.test_review_references import _consumed_reference

    refs, _resolution = _consumed_reference(tmp_path, phase_source, monkeypatch, schema=2, execute=False)
    plan_path = Path(refs['plan']['file'])
    value = json.loads(plan_path.read_text())
    material = Path(next(iter(json.loads((Path(value['arms']['candidate']) / 'manifest.json').read_text())['originals'])))
    row = {'plan': refs['plan'], 'checklist': refs['checklist'], 'expectations': refs['freeze'],
           'baseline_checklist': refs['checklist'], 'baseline_expectations': refs['freeze'],
           'condition_differences': []}
    if fault == 'missing_baseline':
        row.pop('baseline_checklist')
    else:
        checklist = json.loads(Path(refs['checklist']['file']).read_text())
        if fault == 'candidate_rule':
            wrong = tmp_path / 'wrong-rule.md'
            wrong.write_text('Changed original rule')
            wrong.chmod(0o600)
            checklist['rule_refs'][0] = ref(wrong)
        changed = tmp_path / 'final-checklist.json'
        save(changed, checklist)
        row['checklist'] = ref(changed)
        freeze = json.loads(Path(refs['freeze']['file']).read_text())
        freeze.update(checklist_sha256=row['checklist']['sha256'])
        if fault == 'late_freeze':
            freeze['frozen_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        changed_freeze = tmp_path / 'final-expectations.json'
        save(changed_freeze, freeze)
        row['expectations'] = ref(changed_freeze)
    bank = tmp_path / 'bank.json'
    save(bank, {'cases': [{'alias': f'T{i}', 'stable_identity_sha256': f'{i:064x}', 'material': ref(material)}
                         for i in range(20)]})
    cases = [{**row, 'alias': f'T{i}', 'stable_identity_sha256': f'{i:064x}'} for i in range(20)]
    scope = tmp_path / 'final-scope.json'
    save(scope, {'purpose': 'actual_version_comparison_not_isolated_input_effect',
                 'identity_bank': ref(bank), 'baseline': value['source_revisions']['baseline'],
                 'candidate': value['source_revisions']['candidate'], 'cases': cases,
                 'oa_write_authorized': False, 'automatic_retries': 0,
                 'maximum_per_arm': 1, 'timeout_seconds_per_arm': 900})
    adapter = ROOT / 'docs/agents/templates/reference-pair-adapter.private.py'
    caller = _ComparisonCaller(caller_file=Path(__file__), identity_bank=ref(bank), adapter_file=ref(adapter))

    with pytest.raises((KeyError, ValueError)) as error:
        caller.verify_scope(ref(scope))

    assert str(error.value) == reason
    assert not plan_path.with_name(plan_path.name + '.started.json').exists()


@pytest.mark.parametrize('foreign_parent', [False, True])
def test_source_qualification_cannot_escape_the_original_parent_chain(tmp_path, foreign_parent):
    from apps.oa_finance_preaudit.review_comparison import _ComparisonCaller
    from tests.finance_preaudit.test_review_experiment import save

    parent = tmp_path / 'parent.json'
    save(parent, {'includes_previous_starts': 0})
    origin_scope, origin_authority = tmp_path / 'origin-scope.json', tmp_path / 'origin-authority.json'
    save(origin_scope, {'cases': []})
    save(origin_authority, {'scope_sha256': ref(origin_scope)['sha256'],
                           'parent_cumulative_authority': ref(parent), 'prior_plans': []})
    e._bind_candidate_phase_parent(ref(parent), ref(origin_scope), ref(origin_authority))
    other_parent = tmp_path / 'other-parent.json'
    save(other_parent, {'includes_previous_starts': 0, 'different_original_scope': True})
    source_scope = tmp_path / 'unclaimed-source.json'
    save(source_scope, {'candidate': 'neutral-source', 'cases': []})
    source_authority = tmp_path / 'unclaimed-source-authority.json'
    save(source_authority, {'scope_sha256': ref(source_scope)['sha256'],
                          'parent_cumulative_authority': ref(other_parent if foreign_parent else parent)})
    final_authority = tmp_path / 'final-authority.json'
    save(final_authority, {'parent_cumulative_authority': ref(parent)})
    closed = tmp_path / 'closed.json'
    save(closed, {})
    adapter = tmp_path / 'neutral-adapter.py'
    adapter.write_text('"""No financial or model authority in this fixture."""\n')
    adapter.chmod(0o600)
    caller = _ComparisonCaller(caller_file=Path(__file__), identity_bank={}, adapter_file=ref(adapter))
    request = {'authority': ref(final_authority), 'prerequisites': {
        'source_scope': ref(source_scope), 'source_authority': ref(source_authority), 'source_closed': ref(closed)}}

    with pytest.raises(ValueError, match='source_phase_not_in_parent_chain'):
        caller.source_quality(request, {'candidate': 'neutral-source'})

    assert not (tmp_path / 'candidate-phase-claim.private.json').exists()



def test_thin_caller_reports_all_missing_gates_without_native_or_financial_forgery(tmp_path, monkeypatch):
    from tests.finance_preaudit.test_review_experiment import ROOT, save

    caller = tmp_path / 'comparison-caller.private.py'
    caller.write_bytes((ROOT / 'docs/agents/templates/comparison-caller.private.py').read_bytes())
    caller.chmod(0o600)
    adapter = tmp_path / 'neutral-adapter.py'
    adapter.write_text('"""No financial qualification or native execution in this fixture."""\n')
    adapter.chmod(0o600)
    config = tmp_path / 'configuration.json'
    save(config, {'repository': str(ROOT), 'module': ref(ROOT / 'apps/oa_finance_preaudit/review_comparison.py'),
                  'reference_module': ref(ROOT / 'apps/oa_finance_preaudit/review_references.py'),
                  'identity_bank': {}, 'adapter': ref(adapter)})
    request = tmp_path / 'request.json'
    save(request, {'scope': None, 'authority': None, 'prerequisites': {}})
    environment = dict(e.os.environ)
    environment.pop('OA_REVIEW_API_KEY', None)

    output = subprocess.check_output([sys.executable, str(caller), str(ROOT), str(config),
                                      'readiness', str(request)], cwd=ROOT, env=environment, text=True)
    result = json.loads(output)

    assert result['new_model_starts'] == 0 and result['new_oa_writes'] == 0
    assert result['financial_qualification_granted'] is False
    assert result['status'] == 'blocked'
    assert len(result['checks']) == 8
    assert result['checks']['dispatch_wiring']['status'] == 'verified'
    assert not (tmp_path / 'candidate-phase-claim.private.json').exists()
