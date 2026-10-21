"""Exercise local pytest evidence through collection and execution."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]


def run_pytest(tmp_path, source, *, selection=()):
    sample = tmp_path / 'test_sample.py'
    sample.write_text(source)
    expected, result = tmp_path / 'expected.json', tmp_path / 'result.json'
    command = [sys.executable, '-m', 'pytest', '-q', '-c', str(ROOT / 'pytest.ini'),
               '--basetemp', str(tmp_path / 'pytest-workspace'),
               '-p', 'xdist.plugin', '-p', 'scripts.pytest_evidence', str(sample)]
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTEST_ADDOPTS='')
    environment.update(GITHUB_SHA='a' * 40, GITHUB_RUN_ID='1234', GITHUB_RUN_ATTEMPT='1', GITHUB_JOB='quality')
    environment['OA_CI_STARTED_AT'] = str(time.time() - 1000)
    collected = subprocess.run(command + ['--collect-only', '--ci-evidence-file', str(expected)],
                               cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    executed = subprocess.run(command + ['-n', '2', '--dist', 'worksteal', '--max-worker-restart', '0',
        '--ci-expected-file', str(expected), '--ci-evidence-file', str(result), *selection],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)
    assert result.is_file(), executed.stdout + executed.stderr
    return executed, json.loads(result.read_text())


def test_explicit_evidence_verifies_every_case_without_hosted_time_budget(tmp_path):
    execution, report = run_pytest(tmp_path, 'def test_one(): assert True\ndef test_two(): assert True\n')
    assert execution.returncode == 0, execution.stdout + execution.stderr
    assert report['status'] == 'verified'
    assert report['collected_count'] == report['executed_count'] == 2
    assert len(report['workers']) == 2
    assert sum(worker['executed'] for worker in report['workers'].values()) == 2
    assert report['errors'] == []
    assert report['run_attempt'] == 1 and report['job'] == 'quality'
    assert report['timing_scope'] == 'pytest_session'
    assert 'budget_seconds' not in report and 'shard' not in report


@pytest.mark.parametrize(('source', 'selection', 'error'), [
    ('def test_one(): assert True\ndef test_two(): assert True\n', ('-k', 'one'),
     'independent_collection_mismatch'),
    ('import pytest\ndef test_one(): pytest.skip("unexpected")\n', (), 'unexpected_skip'),
    ('def test_one(): assert False\n', (), 'pytest_exit:1'),
    ('import os\ndef test_one(): os._exit(7)\n', (), 'worker_lost:'),
])
def test_explicit_evidence_rejects_incomplete_or_failed_runs(tmp_path, source, selection, error):
    execution, report = run_pytest(tmp_path, source, selection=selection)
    assert execution.returncode != 0
    assert report['status'] == 'failed'
    assert any(value.startswith(error) for value in report['errors'])


@pytest.mark.parametrize('ci_evidence', [False, True])
def test_failure_identity_survives_termination_before_sessionfinish(tmp_path, ci_evidence):
    marker = tmp_path / 'after-failure.ready'
    sample = tmp_path / 'test_sample.py'
    sample.write_text('from pathlib import Path\nimport time\n'
                      'def test_past_failure():\n    assert False\n'
                      f'def test_after_failure():\n    Path({str(marker)!r}).touch()\n    time.sleep(30)\n')
    expected, result = tmp_path / 'collection.json', tmp_path / 'execution.json'
    (tmp_path / 'conftest.py').write_bytes((ROOT / 'tests/conftest.py').read_bytes())
    command = [sys.executable, '-m', 'pytest', '-q', '-c', str(ROOT / 'pytest.ini'),
               '-o', f'cache_dir={tmp_path / "cache"}', '--confcutdir', str(tmp_path), str(sample)]
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTEST_ADDOPTS='')
    environment.update(GITHUB_SHA='a' * 40, GITHUB_RUN_ID='1234', GITHUB_RUN_ATTEMPT='1', GITHUB_JOB='quality')
    collected = subprocess.run(command + ['-p', 'scripts.pytest_evidence', '--collect-only',
                               '--ci-evidence-file', str(expected)],
                               cwd=ROOT, env=environment, capture_output=True, text=True, timeout=15)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    with (tmp_path / 'run.log').open('w') as log:
        options = ['-p', 'scripts.pytest_evidence', '--ci-expected-file', str(expected),
                   '--ci-evidence-file', str(result)] if ci_evidence else []
        process = subprocess.Popen(command + options, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert marker.is_file(), (tmp_path / 'run.log').read_text()
        finally:
            process.terminate()
            process.wait(timeout=10)
    assert process.returncode != 0
    assert not result.exists()
    failures = (result.with_name(result.name + '.failures.jsonl') if ci_evidence else
                next((tmp_path / 'cache' / 'd' / 'failure-records').glob('*.jsonl')))
    records = [json.loads(line) for line in failures.read_text().splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record['nodeid'].endswith('::test_past_failure')
    assert record['phase'] == 'call'
    assert record['collection_sha256'] == json.loads(expected.read_text())['collection_sha256']
    assert (record['code_revision'], record['run_id'], record['run_attempt'], record['job']) == (
        'a' * 40, '1234', 1, 'quality')
    assert 'longrepr' not in record
    if ci_evidence:
        before = failures.read_bytes()
        repeated = subprocess.run(command + options, cwd=ROOT, env=environment,
                                  capture_output=True, text=True, timeout=15)
        assert repeated.returncode != 0
        assert 'ci_evidence_already_exists' in repeated.stderr
        assert failures.read_bytes() == before
        assert not result.exists()
