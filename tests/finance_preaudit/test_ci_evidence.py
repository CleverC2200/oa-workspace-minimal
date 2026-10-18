"""Exercise CI completeness through actual serial collection and xdist execution."""
import json
import hashlib
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
CI_IDENTITY = {'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '1234', 'GITHUB_RUN_ATTEMPT': '1',
               'OA_CI_HEAD_SHA': 'b' * 40}


def job_timings():
    now = time.time()
    jobs = []
    for name, start_age, end_age in [('quality (0)', 600, 480), ('quality (1)', 300, 170),
                                     ('quality (2)', 250, 125),
                                     ('macos-stop-recovery', 400, 375), ('quality-complete', 5, None)]:
        jobs.append({'name': name, 'head_sha': CI_IDENTITY['OA_CI_HEAD_SHA'], 'run_id': 1234, 'run_attempt': 1,
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
    environment.update(GITHUB_SHA='a' * 40, GITHUB_RUN_ID='1234', GITHUB_RUN_ATTEMPT='1', GITHUB_JOB='quality')
    environment['OA_CI_STARTED_AT'] = str(
        json.loads(expected.read_text())['started_at'] if expected.exists() else time.time())
    if not expected.exists():
        collected = subprocess.run(command + ['--collect-only', '--ci-evidence-file', str(expected)],
                                   cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)
        assert collected.returncode == 0, collected.stdout + collected.stderr
    executed = subprocess.run(command + ['-n', '2', '--dist', 'worksteal', '--max-worker-restart', '0',
        '--ci-expected-file', str(expected), '--ci-evidence-file', str(result),
        '--ci-budget-seconds', budget, *selection], cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=30)
    assert result.is_file(), executed.stdout + executed.stderr
    return executed, json.loads(result.read_text())


def test_parallel_ci_accounts_for_every_collected_case(tmp_path):
    execution, report = run_pytest(tmp_path, 'def test_one(): assert True\ndef test_two(): assert True\n')
    assert execution.returncode == 0, execution.stdout + execution.stderr
    assert report['status'] == 'verified'
    assert report['collected_count'] == report['executed_count'] == 2
    assert len(report['workers']) == 2
    assert sum(worker['executed'] for worker in report['workers'].values()) == 2
    assert report['errors'] == []
    assert report['run_attempt'] == 1 and report['job'] == 'quality'


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
    for shard in (0, 1, 2):
        execution, report = run_pytest(tmp_path, source, shard=shard)
        assert execution.returncode == 0, execution.stdout + execution.stderr
        assert report['shard'] == shard
        directory = tmp_path / f'offline-test-evidence-{shard}-attempt-1'
        directory.mkdir()
        shutil.copyfile(tmp_path / 'expected.json', directory / 'collection.json')
        shutil.copyfile(tmp_path / f'result-{shard}.json', directory / 'execution.json')
    directory = tmp_path / 'offline-test-evidence-macos-attempt-1'
    directory.mkdir()
    (directory / 'stop-recovery.xml').write_text('<testsuite/>')
    (directory / 'macos.json').write_text(json.dumps({
        'schema_version': 2, 'job': 'macos-stop-recovery', 'platform': 'darwin',
        'code_revision': 'a' * 40, 'run_id': '1234', 'run_attempt': 1,
        'status': 'verified', 'errors': [], 'started_at': time.time(), 'elapsed_seconds': 0}))
    (tmp_path / 'job-timings.json').write_text(json.dumps(job_timings()))
    return tmp_path


def merge_matrix(directory, output, budget='170', required_results=None, environment_updates=None):
    environment = dict(os.environ)
    environment.update(CI_IDENTITY)
    environment.update(environment_updates or {})
    environment['OA_CI_REQUIRED_RESULTS'] = json.dumps(
        required_results if required_results is not None else attempt_needs((1, 1, 1, 1)))
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


@pytest.mark.parametrize('missing_shard', [0, 1, 2])
def test_current_matrix_requires_every_selected_shard(matrix_evidence, tmp_path, missing_shard):
    directory = tmp_path / 'evidence'
    shutil.copytree(matrix_evidence, directory)
    shutil.rmtree(directory / f'offline-test-evidence-{missing_shard}-attempt-1')
    result = merge_matrix(directory, tmp_path / 'merged.json')
    assert result.returncode != 0, result.stdout + result.stderr


def test_current_matrix_cannot_downgrade_to_two_historical_shards(matrix_evidence, tmp_path):
    directory = tmp_path / 'evidence'
    shutil.copytree(matrix_evidence, directory)
    shutil.rmtree(directory / 'offline-test-evidence-2-attempt-1')
    required = attempt_needs((1, 1, 1, 1))
    del required['quality']['outputs']['attempt_2']
    del required['quality']['outputs']['artifact_2']
    before = {file: file.read_bytes() for file in directory.rglob('*') if file.is_file()}
    result = merge_matrix(directory, tmp_path / 'merged.json', required_results=required)
    assert result.returncode != 0, result.stdout + result.stderr
    assert all(file.read_bytes() == raw for file, raw in before.items())


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'failed', 'collection', 'identity',
                                   'skip', 'macos_skip', 'macos_clock', 'budget', 'extra_shard', 'upstream'])
def test_matrix_ci_rejects_incomplete_reports(matrix_evidence, tmp_path, defect):
    directory = tmp_path / 'evidence'
    shutil.copytree(matrix_evidence, directory)
    execution = directory / 'offline-test-evidence-1-attempt-1/execution.json'
    report = json.loads(execution.read_text())
    if defect == 'missing':
        execution.unlink()
    elif defect == 'collection':
        path = directory / 'offline-test-evidence-1-attempt-1/collection.json'
        reference = json.loads(path.read_text())
        reference['nodeids'].pop()
        path.write_text(json.dumps(reference))
    elif defect == 'macos_skip':
        (directory / 'offline-test-evidence-macos-attempt-1/stop-recovery.xml').write_text(
            '<testsuite><testcase name="missing"><skipped/></testcase></testsuite>')
    elif defect == 'macos_clock':
        path = directory / 'offline-test-evidence-macos-attempt-1/macos.json'
        macos = json.loads(path.read_text())
        macos['started_at'] = float('nan')
        path.write_text(json.dumps(macos))
    elif defect == 'extra_shard':
        (directory / 'offline-test-evidence-3-attempt-1').mkdir()
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
        elif defect == 'skip':
            report['outcomes'][node] = ['skipped']
        execution.write_text(json.dumps(report))
    required = {'quality': {'result': 'success'}, 'macos-stop-recovery': {'result': 'failure'}} if defect == 'upstream' else None
    result = merge_matrix(directory, tmp_path / 'merged.json', '0.001' if defect == 'budget' else '170', required)
    assert result.returncode != 0, result.stdout + result.stderr


def attempt_artifacts(directory, attempt, *, started_at=None, failed_shard=None):
    """Small recorded artifact set; the existing tests exercise actual xdist above."""
    started_at = time.time() - 2 if started_at is None else started_at
    manual = ('tests/finance_preaudit/test_manual_stop_recovery.py::'
              'test_first_maintenance_identity_is_bound_to_original_file_birth')
    nodes = [f'tests/test_recorded.py::test_case_{index}' for index in range(8)] + [manual]
    identity = {'schema_version': 2, 'root': '/offline', 'platform': 'linux',
                'code_revision': 'a' * 40, 'run_id': '1234', 'run_attempt': attempt,
                'job': 'quality', 'started_at': started_at}
    digest = lambda values: hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()
    for shard in (0, 1, 2):
        path = directory / f'offline-test-evidence-{shard}-attempt-{attempt}'
        path.mkdir(parents=True)
        collection = {**identity, 'status': 'collected', 'nodeids': nodes,
                      'collection_sha256': digest(nodes)}
        selected = [node for node in nodes if hashlib.sha256(node.encode()).digest()[0] % 3 == shard]
        outcomes = {node: ['skipped' if node == manual else 'passed'] for node in selected}
        execution = {**identity, 'status': 'verified', 'nodeids': selected, 'shard': shard,
                     'collection_sha256': digest(selected), 'collected_count': len(selected),
                     'executed_count': len(selected), 'outcomes': outcomes, 'errors': [],
                     'elapsed_seconds': 1, 'budget_seconds': 170}
        if shard == failed_shard:
            execution['status'] = 'failed'
            execution['errors'] = ['recorded_test_failure']
            execution['outcomes'][selected[0]] = ['failed']
        (path / 'collection.json').write_text(json.dumps(collection))
        (path / 'execution.json').write_text(json.dumps(execution))
    path = directory / f'offline-test-evidence-macos-attempt-{attempt}'
    path.mkdir()
    (path / 'stop-recovery.xml').write_text(
        f'<testsuite><testcase name="{manual.split("::")[-1]}"/></testsuite>')
    (path / 'macos.json').write_text(json.dumps({**identity, 'job': 'macos-stop-recovery',
        'platform': 'darwin', 'status': 'verified', 'elapsed_seconds': 1,
        'budget_seconds': 170, 'errors': []}))


def attempt_needs(attempts):
    left, right, third, macos = attempts
    return {'quality': {'result': 'success', 'outputs': {
                'attempt_0': str(left), 'artifact_0': str(100 + left),
                'attempt_1': str(right), 'artifact_1': str(200 + right),
                'attempt_2': str(third), 'artifact_2': str(300 + third)}},
            'macos-stop-recovery': {'result': 'success', 'outputs': {
                'attempt': str(macos), 'artifact': str(400 + macos)}}}


def merge_attempt(directory, output, attempt, attempts, *, required=None):
    timings = job_timings()
    for job in timings['jobs']:
        job['run_attempt'] = attempt
    (directory / 'job-timings.json').write_text(json.dumps(timings))
    return merge_matrix(directory, output, required_results=required or attempt_needs(attempts),
        environment_updates={'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '1234',
            'GITHUB_RUN_ATTEMPT': str(attempt)})


def test_partial_rerun_replaces_legacy_failed_artifact_instead_of_reusing_it(tmp_path):
    attempt_artifacts(tmp_path, 1, started_at=time.time() - 600, failed_shard=0)
    attempt_artifacts(tmp_path, 2)
    for role in ('1', '2', 'macos'):
        shutil.rmtree(tmp_path / f'offline-test-evidence-{role}-attempt-2')
    for shard in (0, 1, 2):
        shutil.copytree(tmp_path / f'offline-test-evidence-{shard}-attempt-1',
                        tmp_path / f'offline-test-evidence-{shard}')
    (tmp_path / 'stop-recovery-started-at.txt').write_text(str(time.time() - 600))
    output = tmp_path / 'merged.json'
    result = merge_attempt(tmp_path, output, 2, (2, 1, 1, 1))
    report = json.loads(output.read_text())
    assert report['counts'] == {'passed': 8, 'skipped': 1}, result.stdout
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('attempt,selected', [(1, (1, 1, 1, 1)), (2, (2, 2, 2, 2)), (2, (2, 1, 1, 1)), (2, (1, 1, 2, 1)), (2, (1, 1, 1, 1))])
def test_matrix_uses_job_attempts_for_first_full_partial_and_aggregate_reruns(tmp_path, attempt, selected):
    attempt_artifacts(tmp_path, 1, started_at=time.time() - (600 if attempt > 1 else 2),
                      failed_shard=selected.index(2) if 2 in selected else None)
    if 2 in selected:
        attempt_artifacts(tmp_path, 2)
        for role, wanted in zip(('0', '1', '2', 'macos'), selected):
            if wanted == 1:
                shutil.rmtree(tmp_path / f'offline-test-evidence-{role}-attempt-2')
    (tmp_path / 'complete-ci-evidence').mkdir()
    (tmp_path / 'complete-ci-evidence/stop-recovery.xml').write_text('old aggregate is not an input')
    output = tmp_path / 'merged.json'
    result = merge_attempt(tmp_path, output, attempt, selected)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report['counts'] == {'passed': 8, 'skipped': 1}
    assert report['run_attempt'] == attempt
    assert report['evidence_attempts'] == dict(zip(('quality-0', 'quality-1', 'quality-2', 'macos-stop-recovery'), selected))
    assert 0 <= report['elapsed_seconds'] < 170
    assert report['timing_scope'] == 'active_critical_path'
    durations = dict(zip(('0', '1', '2', 'macos'), (120, 130, 125, 25)))
    current = [durations[role] for role, actual in zip(('0', '1', '2', 'macos'), selected) if actual == attempt]
    assert report['upstream_seconds'] == max(current, default=0)


@pytest.mark.parametrize('defect', ['new_failure', 'missing_new', 'sha', 'run', 'attempt', 'shard',
                                   'job', 'macos_identity', 'macos_skip', 'duplicate',
                                   'output_missing', 'output_future', 'output_stale', 'artifact_duplicate',
                                   'artifact_missing', 'artifact_invalid',
                                   'collection_attempt', 'macos_missing', 'upstream_failure'])
def test_matrix_rerun_never_falls_back_to_old_success(tmp_path, defect):
    attempt_artifacts(tmp_path, 1)
    attempt_artifacts(tmp_path, 2, failed_shard=2 if defect == 'new_failure' else None)
    path = tmp_path / 'offline-test-evidence-2-attempt-2'
    required = attempt_needs((2, 2, 2, 2))
    if defect == 'missing_new':
        shutil.rmtree(path)
    elif defect == 'duplicate':
        shutil.copytree(path, path.with_name(path.name + '-duplicate'))
    elif defect == 'output_missing':
        del required['quality']['outputs']['attempt_2']
    elif defect == 'output_future':
        required['quality']['outputs']['attempt_2'] = '3'
    elif defect == 'output_stale':
        required['quality']['outputs']['attempt_2'] = '1'
    elif defect == 'artifact_duplicate':
        required['quality']['outputs']['artifact_2'] = required['quality']['outputs']['artifact_1']
    elif defect == 'artifact_missing':
        del required['quality']['outputs']['artifact_2']
    elif defect == 'artifact_invalid':
        required['quality']['outputs']['artifact_2'] = '0'
    elif defect == 'upstream_failure':
        required['quality']['result'] = 'failure'
    elif defect == 'macos_skip':
        xml = tmp_path / 'offline-test-evidence-macos-attempt-2/stop-recovery.xml'
        xml.write_text(xml.read_text().replace('/>', '><skipped/></testcase>'))
    elif defect == 'macos_missing':
        (tmp_path / 'offline-test-evidence-macos-attempt-2/macos.json').unlink()
    elif defect not in {'new_failure'}:
        target = (tmp_path / 'offline-test-evidence-macos-attempt-2/macos.json'
                  if defect == 'macos_identity' else path / (
                      'collection.json' if defect == 'collection_attempt' else 'execution.json'))
        value = json.loads(target.read_text())
        key, change = {'sha': ('code_revision', 'b' * 40), 'run': ('run_id', 'other'),
            'attempt': ('run_attempt', 1), 'shard': ('shard', 0), 'job': ('job', 'other'),
            'macos_identity': ('run_attempt', 1), 'collection_attempt': ('run_attempt', 1)}[defect]
        value[key] = change
        target.write_text(json.dumps(value))
    result = merge_attempt(tmp_path, tmp_path / 'merged.json', 2, (2, 2, 2, 2), required=required)
    assert result.returncode != 0, result.stdout + result.stderr


@pytest.mark.parametrize('result_tag', ['', '<skipped/>', '<failure/>', '<error/>', 'empty'])
def test_macos_cli_binds_attempt_and_keeps_complete_execution_gate(tmp_path, result_tag):
    xml, output = tmp_path / 'stop-recovery.xml', tmp_path / 'macos.json'
    xml.write_text('<testsuite/>' if result_tag == 'empty' else
                   f'<testsuite><testcase name="recorded">{result_tag}</testcase></testsuite>')
    environment = dict(os.environ, GITHUB_SHA='a' * 40, GITHUB_RUN_ID='1234', GITHUB_RUN_ATTEMPT='2',
                       GITHUB_JOB='macos-stop-recovery', OA_CI_STARTED_AT=str(time.time()))
    result = subprocess.run([sys.executable, '-m', 'scripts.pytest_evidence',
        '--macos-report', str(xml), '--output', str(output)], cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=30)
    report = json.loads(output.read_text())
    assert report['run_attempt'] == 2 and report['job'] == 'macos-stop-recovery'
    assert report['code_revision'] == 'a' * 40 and report['run_id'] == '1234'
    assert (result.returncode == 0) == (result_tag == '')


def test_rerun_keeps_original_job_budgets_and_current_attempt_budget(tmp_path):
    attempt_artifacts(tmp_path, 1, started_at=time.time() - 600)
    execution = tmp_path / 'offline-test-evidence-1-attempt-1/execution.json'
    original = json.loads(execution.read_text())
    execution.write_text(json.dumps({**original, 'elapsed_seconds': 171}))
    result = merge_attempt(tmp_path, tmp_path / 'invalid-job.json', 2, (1, 1, 1, 1))
    assert result.returncode != 0
    execution.write_text(json.dumps(original))
    timing = job_timings()
    timing['jobs'][-1]['started_at'] = datetime.fromtimestamp(time.time() - 171, timezone.utc).isoformat()
    timing['jobs'][1]['completed_at'] = datetime.fromtimestamp(time.time() - 200, timezone.utc).isoformat()
    timing['jobs'][2]['completed_at'] = datetime.fromtimestamp(time.time() - 200, timezone.utc).isoformat()
    for job in timing['jobs']:
        job['run_attempt'] = 2
    (tmp_path / 'job-timings.json').write_text(json.dumps(timing))
    result = merge_matrix(tmp_path, tmp_path / 'invalid-attempt.json', required_results=attempt_needs((1, 1, 1, 1)),
        environment_updates={'GITHUB_RUN_ATTEMPT': '2'})
    assert result.returncode != 0
    assert 'ci_time_budget_exceeded' in json.loads((tmp_path / 'invalid-attempt.json').read_text())['errors']


def test_matrix_ci_excludes_delayed_shards_and_aggregation_queue(matrix_evidence, tmp_path):
    directory = tmp_path / 'evidence'
    shutil.copytree(matrix_evidence, directory)
    for shard, age in ((0, 600), (1, 300), (2, 250)):
        path = directory / f'offline-test-evidence-{shard}-attempt-1/collection.json'
        reference = json.loads(path.read_text())
        reference['started_at'] = time.time() - age
        path.write_text(json.dumps(reference))
        path.with_name('execution.json').write_text(json.dumps({
            **json.loads(path.with_name('execution.json').read_text()), 'started_at': reference['started_at']}))
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
        job['run_attempt'] = 2
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



@pytest.mark.parametrize('role', ['quality (1)', 'quality (2)', 'macos-stop-recovery'])
def test_reused_job_metadata_still_enforces_original_execution_budget(tmp_path, role):
    attempt_artifacts(tmp_path, 1, started_at=time.time() - 600)
    timing = job_timings()
    for job in timing['jobs']:
        job['run_attempt'] = 2
        if job['name'] == role:
            job['started_at'] = datetime.fromtimestamp(time.time() - 600, timezone.utc).isoformat()
            job['completed_at'] = datetime.fromtimestamp(time.time() - 429, timezone.utc).isoformat()
    (tmp_path / 'job-timings.json').write_text(json.dumps(timing))
    output = tmp_path / 'merged.json'
    result = merge_matrix(tmp_path, output, required_results=attempt_needs((1, 1, 1, 1)),
                          environment_updates={'GITHUB_RUN_ATTEMPT': '2'})
    report = json.loads(output.read_text())
    assert result.returncode != 0, result.stdout + result.stderr
    assert report['errors'] == ['ci_time_budget_exceeded']
    assert report['upstream_seconds'] == 0
    assert report['reused_job_seconds'][role] == pytest.approx(171, abs=0.01)
