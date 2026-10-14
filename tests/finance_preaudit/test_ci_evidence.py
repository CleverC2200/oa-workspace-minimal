"""Exercise CI completeness through actual serial collection and xdist execution."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def run_pytest(tmp_path, source, *, selection=(), budget='170'):
    sample = tmp_path / 'test_sample.py'
    sample.write_text(source)
    expected, result = tmp_path / 'expected.json', tmp_path / 'result.json'
    command = [sys.executable, '-m', 'pytest', '-q', '-c', str(ROOT / 'pytest.ini'),
               '-p', 'xdist.plugin', '-p', 'scripts.pytest_evidence', str(sample)]
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTEST_ADDOPTS='')
    environment.pop('OA_CI_STARTED_AT', None)
    collected = subprocess.run(command + ['--collect-only', '--ci-evidence-file', str(expected)],
                               cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    executed = subprocess.run(command + ['-n', '2', '--dist', 'worksteal', '--max-worker-restart', '0',
        '--ci-expected-file', str(expected), '--ci-evidence-file', str(result),
        '--ci-budget-seconds', budget, *selection], cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=30)
    return executed, json.loads(result.read_text())


def test_parallel_ci_accounts_for_every_collected_case(tmp_path):
    execution, report = run_pytest(tmp_path, 'def test_one(): assert True\ndef test_two(): assert True\n')
    assert execution.returncode == 0, execution.stdout + execution.stderr
    assert report['status'] == 'verified'
    assert report['collected_count'] == report['executed_count'] == 2
    assert len(report['workers']) == 2
    assert sum(worker['executed'] for worker in report['workers'].values()) == 2
    assert report['errors'] == []


@pytest.mark.parametrize(('source', 'selection', 'budget', 'error'), [
    ('def test_one(): assert True\ndef test_two(): assert True\n', ('-k', 'one'),
     '170', 'independent_collection_mismatch'),
    ('import pytest\ndef test_one(): pytest.skip("unexpected")\n', (),
     '170', 'unexpected_skip'),
    ('def test_one(): assert False\n', (), '170', 'pytest_exit:1'),
    ('def test_one(): assert True\n', (), '0.001', 'ci_time_budget_exceeded'),
    ('import os\ndef test_one(): os._exit(7)\n', (), '170', 'worker_lost:'),
])
def test_ci_evidence_rejects_incomplete_or_failed_runs(tmp_path, source, selection, budget, error):
    execution, report = run_pytest(tmp_path, source, selection=selection, budget=budget)
    assert execution.returncode != 0
    assert report['status'] == 'failed'
    assert any(value.startswith(error) for value in report['errors'])
