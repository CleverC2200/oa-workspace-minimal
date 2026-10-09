"""Preflight inspection lists every missing prerequisite; it grants no authority."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from apps.oa_finance_preaudit import review_experiment as experiment


CHECKS = ('scope', 'authority', 'source_quality', 'financial_qualification',
          'selection', 'budget', 'native_contract', 'dispatch_wiring')


def test_inspection_collects_independent_failures_without_short_circuit():
    observed = []

    def check(name):
        def run():
            observed.append(name)
            if name in {'source_quality', 'dispatch_wiring'}:
                raise ValueError(name + '_unverified')
        return run

    report = experiment._comparison_readiness({name: check(name) for name in CHECKS})
    assert observed == list(CHECKS)
    assert report['blocking_checks'] == ['source_quality', 'dispatch_wiring']
    assert report['status'] == 'blocked'
    assert report['checks']['budget']['status'] == 'verified'
    assert all(check['seconds'] >= 0 for check in report['checks'].values())


@pytest.mark.parametrize('missing', CHECKS)
def test_missing_prerequisite_cannot_become_ready(missing):
    checks = {name: lambda: None for name in CHECKS if name != missing}
    report = experiment._comparison_readiness(checks)
    assert report['blocking_checks'] == [missing]
    assert report['checks'][missing]['reason'] == 'check_not_supplied'
    assert report['status'] == 'blocked'


def test_successful_inspection_is_not_execution_authority():
    report = experiment._comparison_readiness({name: lambda: None for name in CHECKS})
    assert report['status'] == 'prerequisites_verified'
    assert report['execution_authority_granted'] is False
    assert report['new_model_starts'] == 0
    assert report['new_oa_writes'] == 0
    assert report['requires_run_time_revalidation'] is True


def test_unexpected_program_failure_is_not_hidden_as_missing_evidence():
    def broken():
        raise RuntimeError('unexpected bug')
    with pytest.raises(RuntimeError, match='unexpected bug'):
        experiment._comparison_readiness({'scope': broken})


def test_unknown_check_is_rejected():
    with pytest.raises(ValueError, match='comparison_readiness_checks_invalid'):
        experiment._comparison_readiness({'invented_pass': lambda: None})


def test_unchecked_result_cannot_be_treated_as_verified():
    report = experiment._comparison_readiness({'scope': lambda: {'status': 'unverified'}})
    assert report['checks']['scope']['status'] == 'unverified'
    assert report['checks']['scope']['reason'] == 'check_result_invalid'


def bound_authority(tmp_path, *, authority_limit=76, parent_limit=76):
    from apps.oa_finance_preaudit.review_comparison import _ComparisonCaller
    from tests.finance_preaudit.test_review_experiment import save
    from tests.finance_preaudit.test_candidate_phase import ref

    adapter = tmp_path / 'neutral-adapter.py'
    adapter.write_text('"""Authority-only fixture; never qualify or dispatch."""\n')
    adapter.chmod(0o600)
    caller_file = Path(__file__)
    caller = _ComparisonCaller(caller_file=caller_file, identity_bank={}, adapter_file=ref(adapter))
    scope = tmp_path / 'scope.json'
    save(scope, {'kind': 'authority-only-fixture'})
    parent = tmp_path / 'parent.json'
    save(parent, {'maximum_cli_starts': 120, 'oa_write_authorized': False,
                  'phase_limits': {'candidate_diagnosis': parent_limit},
                  'same_source_model_retries': False, 'quality_failure_stops_revision_expansion': True})
    now = datetime.now(timezone.utc)
    authority = tmp_path / 'authority.json'
    save(authority, {'scope_file': str(scope), 'scope_sha256': ref(scope)['sha256'],
                     'parent_cumulative_authority': ref(parent), 'runner_sha256': ref(caller_file)['sha256'],
                     'phase': 'final_production_code_comparison', 'oa_write_authorized': False,
                     'cumulative_maximum_cli_starts': 120, 'candidate_diagnosis_limit': authority_limit,
                     'maximum_cli_starts': 32, 'authority_reference': 'Explicit approved fixture authority',
                     'not_before': (now - timedelta(minutes=1)).isoformat(),
                     'expires_at': (now + timedelta(hours=1)).isoformat()})
    return caller, ref(scope), ref(authority)


@pytest.mark.parametrize('diagnosis_limit', [76, 80])
def test_final_authority_accepts_bound_approved_limit_without_new_starts(tmp_path, diagnosis_limit):
    caller, scope, authority = bound_authority(tmp_path, authority_limit=diagnosis_limit, parent_limit=diagnosis_limit)
    result = caller.authority_for(scope, authority)
    assert result['candidate_diagnosis_limit'] == diagnosis_limit
    assert result['cumulative_maximum_cli_starts'] == 120
    assert not (tmp_path / 'candidate-phase-claim.private.json').exists()


def test_original_bound_70_authority_remains_readable(tmp_path):
    caller, scope, authority = bound_authority(tmp_path, authority_limit=70, parent_limit=70)
    assert caller.authority_for(scope, authority)['candidate_diagnosis_limit'] == 70


@pytest.mark.parametrize('authority_limit,parent_limit', [(76, 70), (70, 76), (80, 76), (76, 80),
                                                         (75, 75), (77, 77), (81, 81),
                                                         (120, 120), (76.0, 76), (76, 76.0)])
def test_final_authority_rejects_mismatch_or_unapproved_limit(tmp_path, authority_limit, parent_limit):
    caller, scope, authority = bound_authority(tmp_path, authority_limit=authority_limit, parent_limit=parent_limit)
    with pytest.raises(ValueError, match='^authorization_not_bound$'):
        caller.authority_for(scope, authority)


def authentication_plan(tmp_path, *, provider='openai', change=None):
    from tests.finance_preaudit.test_review_experiment import save
    from tests.finance_preaudit.test_candidate_phase import ref

    identity = {'model': 'gpt-6.1-sol', 'provider': provider, 'reasoning_effort': 'medium',
                'requested_service_tier': 'default'}
    if change == 'missing':
        identity = None
    elif change == 'incomplete':
        identity.pop('requested_service_tier')
    elif change == 'empty':
        identity['reasoning_effort'] = ''
    elif change == 'extra':
        identity['inferred_from_model'] = True
    plan = tmp_path / 'auth-plan.json'
    save(plan, {'preparations': {'candidate': {'conditions': {'call_identity': identity}}}})
    return ref(plan)


def test_bound_openai_identity_uses_native_auth_without_deepseek_key(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit.review_comparison import _require_reviewer_authentication

    monkeypatch.delenv('OA_REVIEW_API_KEY', raising=False)
    plan = authentication_plan(tmp_path)
    assert _require_reviewer_authentication(plan, ['candidate']) is None
    assert not (tmp_path / 'auth.json').exists()


@pytest.mark.parametrize('key_present', [False, True])
def test_bound_deepseek_identity_keeps_its_key_requirement(tmp_path, monkeypatch, key_present):
    from apps.oa_finance_preaudit.review_comparison import _require_reviewer_authentication

    monkeypatch.delenv('OA_REVIEW_API_KEY', raising=False)
    if key_present:
        monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    plan = authentication_plan(tmp_path, provider='deepseek')
    if key_present:
        assert _require_reviewer_authentication(plan, ['candidate']) is None
    else:
        with pytest.raises(ValueError, match='^reviewer_credentials_unavailable$'):
            _require_reviewer_authentication(plan, ['candidate'])


@pytest.mark.parametrize('provider,change', [('unknown', None), ('oa_deepseek', None), ('openai', 'missing'),
                                           ('openai', 'incomplete'), ('openai', 'empty'), ('openai', 'extra')])
def test_authentication_does_not_guess_from_model_or_incomplete_identity(tmp_path, monkeypatch, provider, change):
    from apps.oa_finance_preaudit.review_comparison import _require_reviewer_authentication

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    plan = authentication_plan(tmp_path, provider=provider, change=change)
    with pytest.raises(ValueError, match='^reviewer_identity_unverified$'):
        _require_reviewer_authentication(plan, ['candidate'])


def test_authentication_rejects_changed_bound_plan_before_dispatch(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit.review_comparison import _require_reviewer_authentication

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    plan = authentication_plan(tmp_path)
    Path(plan['file']).write_text('{}')
    with pytest.raises(ValueError):
        _require_reviewer_authentication(plan, ['candidate'])


def test_caller_rechecks_frozen_source_host_without_changing_verifier(tmp_path):
    from apps.oa_finance_preaudit.review_comparison import _ComparisonCaller
    from tests.finance_preaudit.test_candidate_phase import ref

    adapter = tmp_path / 'neutral-adapter.py'
    adapter.write_text('"""No dispatch or financial judgment."""\n')
    adapter.chmod(0o600)
    host = tmp_path / 'source-host.py'
    host.write_text('"""Bound transport only."""\n')
    host.chmod(0o600)
    host_reference = ref(host)
    caller = _ComparisonCaller(caller_file=Path(__file__), identity_bank={},
                               adapter_file=ref(adapter), source_host=host_reference)
    caller._verify_runtime()

    host.write_text('"""Changed after the original binding."""\n')
    host_reference.update(ref(host))
    with pytest.raises(ValueError):
        caller._verify_runtime()

    assert not (tmp_path / 'candidate-phase-claim.private.json').exists()
