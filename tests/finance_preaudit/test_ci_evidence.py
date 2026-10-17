"""Exercise CI completeness through actual serial collection and xdist execution."""
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
CI_IDENTITY = {'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
               'OA_CI_HEAD_SHA': 'b' * 40}


def job_timings():
    now = time.time()
    jobs = []
    for name, start_age, end_age in [('quality (0)', 600, 480), ('quality (1)', 300, 170),
                                     ('macos-stop-recovery', 400, 375), ('quality-complete', 5, None)]:
        jobs.append({'name': name, 'head_sha': CI_IDENTITY['OA_CI_HEAD_SHA'], 'run_id': 123, 'run_attempt': 2,
                     'status': 'in_progress' if end_age is None else 'completed',
                     'conclusion': None if end_age is None else 'success',
                     'started_at': datetime.fromtimestamp(now - start_age, timezone.utc).isoformat(),
                     'completed_at': None if end_age is None else
                     datetime.fromtimestamp(now - end_age, timezone.utc).isoformat()})
    return {'total_count': len(jobs), 'jobs': jobs}


def run_pytest(tmp_path, source, *, selection=(), budget='170', shard=None):
    sample = tmp_path / 'test_sample.py'
    sample.write_text(source)
    expected, result = tmp_path / 'expected.json', tmp_path / 'result.json'
    if shard is not None:
        result = tmp_path / f'result-{shard}.json'
        selection = (*selection, '--ci-shard', str(shard))
    command = [sys.executable, '-m', 'pytest', '-q', '-c', str(ROOT / 'pytest.ini'),
               '-p', 'xdist.plugin', '-p', 'scripts.pytest_evidence', str(sample)]
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTEST_ADDOPTS='')
    environment.update(CI_IDENTITY)
    environment.pop('OA_CI_STARTED_AT', None)
    if not expected.exists():
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


@pytest.fixture(scope='module')
def matrix_evidence(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp('matrix-evidence')
    source = '\n'.join(f'def test_case_{index}(): assert True' for index in range(32))
    for shard in (0, 1):
        execution, report = run_pytest(tmp_path, source, shard=shard)
        assert execution.returncode == 0, execution.stdout + execution.stderr
        assert report['shard'] == shard
        directory = tmp_path / f'offline-test-evidence-{shard}'
        directory.mkdir()
        shutil.copyfile(tmp_path / 'expected.json', directory / 'collection.json')
        shutil.copyfile(tmp_path / f'result-{shard}.json', directory / 'execution.json')
    (tmp_path / 'job-timings.json').write_text(json.dumps(job_timings()))
    return tmp_path


def merge_matrix(directory, output, budget='170', required_results=None):
    environment = dict(os.environ)
    environment.update(CI_IDENTITY)
    if required_results is not None:
        environment['OA_CI_REQUIRED_RESULTS'] = json.dumps(required_results)
    return subprocess.run([sys.executable, '-m', 'scripts.pytest_evidence',
        '--merge-directory', str(directory), '--output', str(output), '--budget-seconds', budget,
        '--jobs-file', str(directory / 'job-timings.json')],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)


def test_matrix_ci_merges_exact_full_coverage(matrix_evidence, tmp_path):
    output = tmp_path / 'merged.json'
    result = merge_matrix(matrix_evidence, output)
    assert result.returncode == 0, result.stdout + result.stderr
    merged = json.loads(output.read_text())
    assert merged['status'] == 'verified'
    assert merged['collected_count'] == merged['executed_count'] == 32
    assert merged['counts'] == {'passed': 32}


def test_matrix_ci_excludes_delayed_shards_and_aggregation_queue(matrix_evidence, tmp_path):
    directory = tmp_path / 'evidence'
    shutil.copytree(matrix_evidence, directory)
    for shard, age in ((0, 600), (1, 300)):
        path = directory / f'offline-test-evidence-{shard}/collection.json'
        reference = json.loads(path.read_text())
        reference['started_at'] = time.time() - age
        path.write_text(json.dumps(reference))
    output = tmp_path / 'merged.json'
    result = merge_matrix(directory, output)
    report = json.loads(output.read_text())
    assert result.returncode == 0, result.stdout + result.stderr
    assert report['status'] == 'verified'
    assert report['elapsed_seconds'] < 170
    assert report['timing_scope'] == 'active_critical_path'
    assert report['upstream_seconds'] == 130
    assert report['aggregation_seconds'] >= 5
    assert report['matrix_wall_seconds'] >= 600
    assert report['excluded_scheduling_seconds'] >= 400
    assert report['elapsed_seconds'] == pytest.approx(report['upstream_seconds'] + report['aggregation_seconds'])


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'failed', 'collection', 'identity',
                                   'skip', 'macos_skip', 'budget', 'extra_shard', 'upstream', 'attempt'])
def test_matrix_ci_rejects_incomplete_reports(matrix_evidence, tmp_path, defect):
    directory = tmp_path / 'evidence'
    shutil.copytree(matrix_evidence, directory)
    execution = directory / 'offline-test-evidence-1/execution.json'
    report = json.loads(execution.read_text())
    if defect == 'missing':
        execution.unlink()
    elif defect == 'collection':
        path = directory / 'offline-test-evidence-1/collection.json'
        reference = json.loads(path.read_text())
        reference['nodeids'].pop()
        path.write_text(json.dumps(reference))
    elif defect == 'macos_skip':
        (directory / 'stop-recovery.xml').write_text('<testsuite><testcase name="missing"><skipped/></testcase></testsuite>')
    elif defect == 'extra_shard':
        (directory / 'offline-test-evidence-2').mkdir()
    elif defect in {'budget', 'upstream'}:
        pass
    else:
        node = next(iter(report['outcomes']))
        if defect == 'duplicate':
            report['outcomes'][node].append('passed')
        elif defect == 'failed':
            report['status'] = 'failed'
        elif defect == 'identity':
            report['code_revision'] = 'different-source'
        elif defect == 'attempt':
            report['run_attempt'] = '1'
        elif defect == 'skip':
            report['outcomes'][node] = ['skipped']
        execution.write_text(json.dumps(report))
    required = {'quality': {'result': 'success'}, 'macos-stop-recovery': {'result': 'failure'}} if defect == 'upstream' else None
    result = merge_matrix(directory, tmp_path / 'merged.json', '0.001' if defect == 'budget' else '170', required)
    assert result.returncode != 0, result.stdout + result.stderr


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'truncated', 'failed', 'running',
                                   'run', 'attempt', 'head', 'clock', 'timezone', 'future',
                                   'upstream_budget', 'aggregation_budget'])
def test_matrix_ci_requires_bound_job_execution_timing(matrix_evidence, tmp_path, defect):
    directory = tmp_path / 'evidence'
    shutil.copytree(matrix_evidence, directory)
    timing = job_timings()
    job = timing['jobs'][1]
    if defect == 'missing':
        timing['jobs'].pop()
        timing['total_count'] -= 1
    elif defect == 'duplicate':
        timing['jobs'][0] = job
    elif defect == 'truncated':
        timing['total_count'] += 1
    elif defect == 'failed':
        job['conclusion'] = 'failure'
    elif defect == 'running':
        job['status'] = 'in_progress'
    elif defect == 'run':
        job['run_id'] = 124
    elif defect == 'attempt':
        job['run_attempt'] = 1
    elif defect == 'head':
        job['head_sha'] = CI_IDENTITY['GITHUB_SHA']
    elif defect == 'clock':
        job['completed_at'] = job['started_at']
        job['started_at'] = timing['jobs'][-1]['started_at']
    elif defect == 'timezone':
        job['started_at'] = '2026-10-17T00:00:00'
    elif defect == 'future':
        timing['jobs'][-1]['started_at'] = '9999-01-01T00:00:00Z'
    elif defect == 'upstream_budget':
        job['started_at'] = datetime.fromtimestamp(time.time() - 600, timezone.utc).isoformat()
        job['completed_at'] = datetime.fromtimestamp(time.time() - 400, timezone.utc).isoformat()
    elif defect == 'aggregation_budget':
        timing['jobs'][-1]['started_at'] = datetime.fromtimestamp(time.time() - 50, timezone.utc).isoformat()
    (directory / 'job-timings.json').write_text(json.dumps(timing))
    output = tmp_path / 'merged.json'
    result = merge_matrix(directory, output)
    assert result.returncode != 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    if defect.endswith('_budget'):
        assert report['errors'] == ['ci_time_budget_exceeded']
    else:
        assert any(error.startswith('job_timing_') for error in report['errors'])
