"""Readonly preparation and complete financial qualification have distinct states.

Neutral subprocess archives exercise binding contracts, never financial truth.
"""
from datetime import datetime, timedelta, timezone
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from apps.oa_finance_preaudit import flow_evals, review_batch_report as batch, review_experiment as experiment
from apps.oa_finance_preaudit.review_comparison import _ComparisonCaller
from tests.finance_preaudit.test_candidate_phase import phase_source, ref  # noqa: F401
from tests.finance_preaudit.test_review_experiment import save
from tests.finance_preaudit.test_review_references import _consumed_reference, _review
from tests.finance_preaudit.source_review_fixture import source_review


ROOT = Path(__file__).resolve().parents[2]


def read(reference):
    return json.loads(Path(reference['file']).read_text())


@pytest.fixture(scope='module')
def completed_controls(tmp_path_factory, phase_source):
    directory = tmp_path_factory.mktemp('readonly-controls')
    monkeypatch = pytest.MonkeyPatch()
    try:
        records, rows = [], []
        for index in range(4):
            case = directory / str(index)
            case.mkdir(mode=0o700)
            refs, _resolution = _consumed_reference(
                case, phase_source, monkeypatch, schema=3 if index == 0 else 2,
                request_id=f'neutral-control-{index}',
                scenario='confirmed_exception_with_unknown' if index == 3 else 'ordinary_payment')
            records.append(refs)
            plan = read(refs['plan'])
            audit = case / 'candidate-audit-binding.private.json'
            save(audit, {'quality_report': refs['quality'], 'assessment': refs['assessment'],
                         'checklist': refs['checklist']})
            rows.append({'alias': f'T{index}', 'plan': refs['plan'],
                         'checklist': refs['checklist'], 'expectations': refs['freeze'],
                         'audit_binding': ref(audit),
                         'stable_identity_sha256': plan['preparations']['candidate']['conditions']['sample']})
        scope = directory / 'source-scope.json'
        save(scope, {'schema_version': 1, 'kind': 'preaudit_private_candidate_phase',
                     'candidate': plan['source_revisions']['candidate'], 'cases': rows,
                     'condition_differences': []})
        parent = directory / 'parent.json'
        save(parent, {'maximum_cli_starts': 120, 'oa_write_authorized': False,
                      'phase_limits': {'candidate_diagnosis': 76}, 'includes_previous_starts': 0,
                      'same_source_model_retries': False, 'quality_failure_stops_revision_expansion': True})
        authority = directory / 'source-authority.json'
        now = datetime.now(timezone.utc)
        save(authority, {'scope_sha256': ref(scope)['sha256'], 'parent_cumulative_authority': ref(parent),
                         'prior_plans': [], 'runner_sha256': ref(Path(__file__))['sha256'],
                         'maximum_cli_starts': 4, 'cumulative_maximum_cli_starts': 120,
                         'candidate_diagnosis_limit': 76, 'oa_write_authorized': False,
                         'baseline_execution_authorized': False, 'first_quality_or_format_failure_stops': True,
                         'not_before': (now - timedelta(minutes=1)).isoformat(),
                         'expires_at': (now + timedelta(hours=1)).isoformat(),
                         'authority_reference': 'Neutral contract rehearsal only.'})
        experiment._bind_candidate_phase_parent(ref(parent), ref(scope), ref(authority))
        experiment._finish_candidate_phase(scope, authority)
        quality = directory / 'source-financial-quality.json'
        save(quality, flow_evals.build_financial_quality_report([_review(r) for r in records]))
        final_authority = directory / 'final-authority.json'
        save(final_authority, {'parent_cumulative_authority': ref(parent)})
        closed = directory / 'phase-report-closed.private.json'
        request = {'authority': ref(final_authority), 'prerequisites': {
            'source_scope': ref(scope), 'source_authority': ref(authority), 'source_closed': ref(closed),
            'source_phase': read(ref(closed))['report'], 'financial_qualification': ref(quality)}}
        caller = _ComparisonCaller(caller_file=Path(__file__), identity_bank={},
            adapter_file=ref(ROOT / 'docs/agents/templates/reference-pair-adapter.private.py'))
        yield caller, request, read(ref(scope)), records
    finally:
        monkeypatch.undo()


def test_four_current_controls_can_prepare_readonly_comparison_without_full_qualification(completed_controls):
    caller, request, scope, records = completed_controls
    before = {Path(r['quality']['file']): Path(r['quality']['file']).read_bytes() for r in records}

    ready = caller.readonly_start_ready(request, scope)

    assert ready['status'] == 'readonly_start_ready'
    assert ready['source_reported_eligible'] is False
    assert ready['financial_qualification'] is False
    assert ready['new_model_starts'] == ready['new_oa_writes'] == 0
    assert read(request['prerequisites']['financial_qualification'])['eligible'] is False
    with pytest.raises(ValueError, match='^full_source_financial_qualification_missing$'):
        caller.full_source_qualification(request)
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_readonly_inspection_uses_expectations_gate_without_claiming_qualification():
    checks = ('scope', 'authority', 'source_quality', 'source_expectations',
              'selection', 'budget', 'native_contract', 'dispatch_wiring')

    ready = experiment._comparison_readiness({name: lambda: None for name in checks})

    assert ready['status'] == 'prerequisites_verified'
    assert ready['checks']['source_expectations']['status'] == 'verified'
    assert 'financial_qualification' not in ready['checks']
    assert ready['execution_authority_granted'] is False
    assert ready['new_model_starts'] == ready['new_oa_writes'] == 0


@pytest.mark.parametrize('fault', ['dropped_case', 'changed_check', 'unqualified_expectations'])
def test_readonly_start_rejects_aggregate_scores_detached_from_original_audits(
        completed_controls, tmp_path, fault):
    caller, original_request, scope, _records = completed_controls
    request = copy.deepcopy(original_request)
    quality = read(request['prerequisites']['financial_qualification'])
    if fault == 'dropped_case':
        quality['cases'].pop()
    elif fault == 'changed_check':
        quality['cases'][1]['check_outcomes']['minimum'] = 'fail'
    else:
        quality['cases'][1]['window_expectations_qualified'] = False
    changed = tmp_path / 'changed-quality.json'
    save(changed, quality)
    request['prerequisites']['financial_qualification'] = ref(changed)

    with pytest.raises(ValueError, match='^source_quality_(window_mismatch|expectations_unqualified)$'):
        caller.readonly_start_ready(request, scope)


def test_closed_report_cannot_replace_a_stopped_control_phase_with_complete(completed_controls, tmp_path):
    caller, original_request, scope, _records = completed_controls
    request = copy.deepcopy(original_request)
    report = read(request['prerequisites']['source_phase'])
    report['phase']['status'] = 'stopped'
    changed_phase = tmp_path / 'changed-phase.json'
    save(changed_phase, report)
    closed = read(request['prerequisites']['source_closed'])
    closed['report'] = ref(changed_phase)
    changed_closed = tmp_path / 'changed-closed.json'
    save(changed_closed, closed)
    request['prerequisites'].update(source_phase=ref(changed_phase), source_closed=ref(changed_closed))

    with pytest.raises(ValueError, match='^source_phase_not_complete$'):
        caller.readonly_start_ready(request, scope)


def test_control_scope_cannot_be_shrunk_to_bypass_its_frozen_parent(completed_controls, tmp_path):
    caller, original_request, scope, _records = completed_controls
    request = copy.deepcopy(original_request)
    source_scope = read(request['prerequisites']['source_scope'])
    source_scope['cases'] = source_scope['cases'][:1]
    changed = tmp_path / 'one-case-scope.json'
    save(changed, source_scope)
    request['prerequisites']['source_scope'] = ref(changed)

    with pytest.raises(ValueError, match='^source_phase_not_in_parent_chain$'):
        caller.readonly_start_ready(request, scope)


def test_readonly_and_full_qualification_gates_cannot_be_combined_as_a_bypass():
    with pytest.raises(ValueError, match='^comparison_readiness_checks_invalid$'):
        experiment._comparison_readiness({'source_expectations': lambda: None,
                                          'financial_qualification': lambda: None})


def baseline_audit(directory, plan_ref, candidate_checklist, monkeypatch, *, outcome='pass', execute=True):
    """Create an independent neutral baseline audit after its own actual preview."""
    plan = read(plan_ref)
    root = Path(plan['arms']['baseline'])
    manifest = read(ref(root / 'manifest.json'))
    checklist = read(candidate_checklist)
    checklist['source_refs'] = [ref(Path(name)) for name in manifest['originals']]
    rules = ('SKILL.md', 'references/result-contract.md', 'references/observation-contract.md',
             'references/flow-engine-contract.md', 'references/flows/business-entertainment.md')
    checklist['rule_refs'] = [ref(root / 'calling-source/.agents/skills/oa-finance-preaudit' / name)
                              for name in rules]
    if outcome == 'unverified':
        checklist['business_scope_is_unique'] = False
    checklist_path = directory / 'baseline-checklist.json'
    save(checklist_path, checklist)
    checklist_ref = ref(checklist_path)
    freeze = flow_evals.freeze_financial_expectations(
        checklist_ref, directory / 'baseline-expectations.json',
        source_review=source_review(directory, checklist_ref))
    if not execute:
        return checklist_ref, freeze, None
    plan_path = Path(plan_ref['file'])
    claim = plan_path.with_name(plan_path.name + '.started.json')
    if not claim.exists():
        save(claim, {'plan_sha256': plan_ref['sha256']})
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    result = experiment._host(Path(plan_ref['file']), 'baseline', execute=True)
    assert result['cli_starts'] == 1 and result['write_state'] == 'not_requested'
    report = batch.build_report(root)
    action = Path(manifest['action_file'])
    assessment = directory / 'baseline-assessment.json'
    save(assessment, {'schema_version': 1, 'method': 'independent_manual',
        'reviewer_role': 'historical_crosscheck', 'reviewer_record': 'neutral-baseline-output-audit',
        'reviewed_at': datetime.now(timezone.utc).isoformat(),
        'item_key': report['financial_binding']['item_key'],
        'archive_manifest_sha256': ref(root / 'manifest.json')['sha256'],
        'result_sha256': ref(root / 'result.json')['sha256'], 'checklist_sha256': checklist_ref['sha256'],
        'output_refs': [ref(p) for p in (root / 'result.json', root / 'reviewer/decision.json',
            action.with_name('review-decision.json'), action.with_name('remark.txt'),
            action.with_name('execution-receipt.json'))],
        'checks': [{'id': check['id'], 'outcome': outcome,
                    'reason': 'Independent neutral original output and consumed preview checked.'}
                   for check in checklist['checks']]})
    quality = directory / 'baseline-quality.json'
    save(quality, flow_evals.build_financial_quality_report([{
        'item_key': report['financial_binding']['item_key'], 'archive': ref(root / 'manifest.json'),
        'checklist': checklist_ref, 'assessment': ref(assessment), 'expectations': freeze}]))
    binding = directory / 'baseline-audit-binding.private.json'
    save(binding, {'quality_report': ref(quality), 'assessment': ref(assessment),
                   'checklist': checklist_ref, 'expectations': freeze})
    return checklist_ref, freeze, ref(binding)


def twenty_fixture(tmp_path_factory, completed_controls, phase_source, *, observed):
    _caller, source_request, source_scope, controls = completed_controls
    directory = tmp_path_factory.mktemp('readonly-twenty')
    monkeypatch = pytest.MonkeyPatch()
    try:
        rows, identities = [], []
        scenarios = ('ordinary_payment', 'shared_payment', 'relationship_unknown',
                     'multiple_payment_refund_overclaim')
        for index in range(20):
            case = directory / str(index)
            case.mkdir(mode=0o700)
            refs, _resolution = _consumed_reference(
                case, phase_source, monkeypatch, schema=2, execute=observed and index >= 3,
                request_id=f'neutral-control-{index}' if index < 3 else f'neutral-final-{index}',
                scenario='ordinary_payment' if index < 3 else scenarios[index % 4])
            plan = read(refs['plan'])
            candidate_refs = controls[index] if index < 3 else refs
            row = {'alias': f'F{index}', 'checklist': candidate_refs['checklist'],
                   'expectations': candidate_refs['freeze'],
                   'condition_differences': ['call_profile'] if index < 3 else []}
            if index < 3:
                original = read(candidate_refs['plan'])
                for field in ('arms', 'preparations', 'manifests'):
                    plan[field]['candidate'] = original[field]['candidate']
                target = case / 'target-plan.json'
                save(target, plan)
                row['reuse'] = {'candidate': candidate_refs}
                row['plan'] = ref(target)
            else:
                row['plan'] = refs['plan']
            baseline_refs = baseline_audit(case, row['plan'], refs['checklist'], monkeypatch,
                outcome='fail' if index == 0 else 'unverified' if index == 1 else 'pass', execute=observed)
            row.update(baseline_checklist=baseline_refs[0], baseline_expectations=baseline_refs[1],
                       stable_identity_sha256=plan['preparations']['candidate']['conditions']['sample'])
            if baseline_refs[2] is not None:
                row['baseline_audit_binding'] = baseline_refs[2]
            if observed and index >= 3:
                audit = case / 'candidate-audit-binding.private.json'
                save(audit, {'quality_report': refs['quality'], 'assessment': refs['assessment'],
                             'checklist': refs['checklist']})
                row['audit_binding'] = ref(audit)
            rows.append(row)
            manifest = read(ref(Path(plan['arms']['baseline']) / 'manifest.json'))
            material = Path(manifest['action_file']).with_name('material.json')
            identities.append({'alias': row['alias'], 'stable_identity_sha256': row['stable_identity_sha256'],
                               'material': ref(material)})
        bank = directory / 'identity-bank.json'
        save(bank, {'cases': identities})
        scope = directory / 'scope.json'
        save(scope, {'purpose': 'actual_version_comparison_not_isolated_input_effect',
            'identity_bank': ref(bank), 'baseline': plan['source_revisions']['baseline'],
            'candidate': source_scope['candidate'], 'cases': rows, 'oa_write_authorized': False,
            'automatic_retries': 0, 'maximum_per_arm': 1, 'timeout_seconds_per_arm': 900})
        authority = directory / 'authority.json'
        now = datetime.now(timezone.utc)
        save(authority, {'scope_file': str(scope), 'scope_sha256': ref(scope)['sha256'],
            'parent_cumulative_authority': read(source_request['authority'])['parent_cumulative_authority'],
            'runner_sha256': ref(Path(__file__))['sha256'], 'phase': 'final_production_code_comparison',
            'oa_write_authorized': False, 'cumulative_maximum_cli_starts': 120,
            'candidate_diagnosis_limit': 76, 'maximum_cli_starts': 37,
            'authority_reference': 'Neutral final qualification rehearsal only.',
            'prior_phase_scope': source_request['prerequisites']['source_scope'],
            'prior_phase_authority': source_request['prerequisites']['source_authority'],
            'prior_plans': [{**r['plan'], 'source_revision': source_scope['candidate'], 'diagnostic': True}
                            for r in controls],
            'not_before': (now - timedelta(minutes=1)).isoformat(),
            'expires_at': (now + timedelta(hours=1)).isoformat()})
        caller = _ComparisonCaller(caller_file=Path(__file__), identity_bank=ref(bank),
            adapter_file=ref(ROOT / 'docs/agents/templates/reference-pair-adapter.private.py'))
        request = {'scope': ref(scope), 'authority': ref(authority),
                   'prerequisites': source_request['prerequisites']}
        yield caller, request
    finally:
        monkeypatch.undo()


@pytest.fixture(scope='module')
def completed_twenty(tmp_path_factory, completed_controls, phase_source):
    yield from twenty_fixture(tmp_path_factory, completed_controls, phase_source, observed=True)


@pytest.fixture(scope='module')
def prepared_twenty(tmp_path_factory, completed_controls, phase_source):
    yield from twenty_fixture(tmp_path_factory, completed_controls, phase_source, observed=False)


def test_actual_run_gate_admits_current_controls_but_keeps_missing_native_proof_blocked(prepared_twenty):
    caller, original_request = prepared_twenty
    request = {**original_request, 'alias': 'F0'}

    with pytest.raises(ValueError, match='^source_native_contract_unavailable_baseline$'):
        caller.validate_or_run(request, run=True)

    scope = read(request['scope'])
    assert read(request['prerequisites']['financial_qualification'])['eligible'] is False
    assert not (Path(request['scope']['file']).parent / 'candidate-phase-claim.private.json').exists()
    for row in scope['cases']:
        plan = read(row['plan'])
        assert not (Path(plan['arms']['baseline']) / 'cli-start.json').exists()


def test_prepared_original_twenty_cannot_receive_financial_qualification(prepared_twenty):
    caller, request = prepared_twenty

    with pytest.raises(ValueError, match='^final_twenty_incomplete$'):
        caller.final_financial_qualification(request)


def test_full_financial_cohort_keeps_original_twenty_and_extra_control_separate(completed_twenty):
    caller, request = completed_twenty

    result = caller.final_financial_qualification(request)

    assert result['financial_twenty_complete'] is True
    assert result['financial_qualification'] is True
    assert result['original_twenty_quality']['eligible'] is False
    assert result['original_twenty_quality']['missing_entertainment_scenarios'] == ['confirmed_exception_with_unknown']
    assert len(result['original_twenty_quality']['cases']) == 20
    assert result['financial_control_cohort']['quality']['eligible'] is True
    assert len(result['financial_control_cohort']['quality']['cases']) == 21
    assert result['financial_control_cohort']['additional_controls'] == ['T3']
    assert result['production_acceptance'] is False
    assert result['new_model_starts'] == result['new_oa_writes'] == 0


def changed_final_request(tmp_path, original_request, scope):
    request = copy.deepcopy(original_request)
    path = tmp_path / 'scope.json'
    save(path, scope)
    authority = read(request['authority'])
    authority.update(scope_file=str(path), scope_sha256=ref(path)['sha256'])
    authority_path = tmp_path / 'authority.json'
    save(authority_path, authority)
    request.update(scope=ref(path), authority=ref(authority_path))
    return request


@pytest.mark.parametrize('fault', ['candidate_failure', 'baseline_unreviewed'])
def test_extra_control_cannot_substitute_for_a_missing_or_failed_twentieth_audit(
        completed_twenty, tmp_path, fault):
    caller, original_request = completed_twenty
    scope = read(original_request['scope'])
    row = scope['cases'][-1]
    if fault == 'baseline_unreviewed':
        binding = read(row['baseline_audit_binding'])
        binding.pop('assessment')
        binding_path = tmp_path / 'incomplete-baseline-binding.json'
        save(binding_path, binding)
        row['baseline_audit_binding'] = ref(binding_path)
    else:
        binding = read(row['audit_binding'])
        assessment = read(binding['assessment'])
        assessment['checks'][0]['outcome'] = 'fail'
        assessment_path = tmp_path / 'failed-assessment.json'
        save(assessment_path, assessment)
        plan = read(row['plan'])
        root = Path(plan['arms']['candidate'])
        quality = flow_evals.build_financial_quality_report([{
            'item_key': assessment['item_key'], 'archive': ref(root / 'manifest.json'),
            'checklist': row['checklist'], 'assessment': ref(assessment_path), 'expectations': row['expectations']}])
        quality_path = tmp_path / 'failed-quality.json'
        save(quality_path, quality)
        binding_path = tmp_path / 'failed-audit-binding.json'
        save(binding_path, {'quality_report': ref(quality_path), 'assessment': ref(assessment_path)})
        row['audit_binding'] = ref(binding_path)
    request = changed_final_request(tmp_path, original_request, scope)

    with pytest.raises((KeyError, ValueError)) as error:
        caller.final_financial_qualification(request)

    assert str(error.value) == ('final_candidate_quality_not_passed' if fault == 'candidate_failure' else "'assessment'")


def test_stopped_revision_cannot_receive_complete_financial_qualification(completed_twenty, tmp_path):
    caller, original_request = completed_twenty
    request = changed_final_request(tmp_path, original_request, read(original_request['scope']))
    save(tmp_path / 'batch-stop.private.json', {'reason': 'controlled_first_failure'})

    with pytest.raises(ValueError, match='^phase_already_stopped$'):
        caller.final_financial_qualification(request)


def test_thin_quality_caller_preserves_twenty_and_control_cohort_without_new_starts(completed_twenty, tmp_path):
    _caller, original_request = completed_twenty
    caller_path = tmp_path / 'comparison-caller.private.py'
    caller_path.write_bytes((ROOT / 'docs/agents/templates/comparison-caller.private.py').read_bytes())
    caller_path.chmod(0o600)
    scope = read(original_request['scope'])
    configuration = tmp_path / 'configuration.json'
    save(configuration, {'repository': str(ROOT), 'module': ref(ROOT / 'apps/oa_finance_preaudit/review_comparison.py'),
        'reference_module': ref(ROOT / 'apps/oa_finance_preaudit/review_references.py'),
        'identity_bank': scope['identity_bank'], 'adapter': ref(ROOT / 'docs/agents/templates/reference-pair-adapter.private.py')})
    request = copy.deepcopy(original_request)
    authority = read(request['authority'])
    authority.update(runner_sha256=ref(caller_path)['sha256'],
                     caller_configuration_sha256=ref(configuration)['sha256'])
    authority_path = tmp_path / 'authority.json'
    save(authority_path, authority)
    request['authority'] = ref(authority_path)
    request_path = tmp_path / 'request.json'
    save(request_path, request)
    environment = dict(os.environ)
    environment.pop('OA_REVIEW_API_KEY', None)
    output = subprocess.check_output([sys.executable, str(caller_path), str(ROOT), str(configuration),
                                      'quality', str(request_path)], cwd=ROOT, env=environment, text=True)
    result = json.loads(output)

    assert result['financial_qualification'] is True
    assert result['original_twenty_quality']['eligible'] is False
    assert len(result['financial_control_cohort']['quality']['cases']) == 21
    assert result['performance_sample_count'] == 20
    assert result['new_model_starts'] == result['new_oa_writes'] == 0
    assert not (tmp_path / 'candidate-phase-claim.private.json').exists()
