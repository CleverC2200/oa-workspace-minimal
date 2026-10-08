"""Preflight inspection lists every missing prerequisite; it grants no authority."""
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
