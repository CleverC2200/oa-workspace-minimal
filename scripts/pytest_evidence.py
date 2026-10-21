"""Opt-in pytest evidence: independent collection, exact execution and time budget."""
from collections import Counter, defaultdict
import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import resource
import secrets
import subprocess
import sys
import time
from xml.etree import ElementTree

import pytest


SHARDS = (0, 1, 2)
_MANUAL_STOP = 'tests/finance_preaudit/test_manual_stop_recovery.py::'
_NATIVE_STAGING = ('tests/finance_preaudit/test_bounded_staging.py::'
                   'test_native_workspace_caps_aggregate_files_and_leaves_no_mount')
_LINUX_SKIPS = {
    _MANUAL_STOP + name for name in (
        'test_first_maintenance_identity_is_bound_to_original_file_birth',
        'test_original_evidence_accepts_system_tmp_alias_without_changing_literal_binding',
        'test_private_evidence_rejects_untrusted_tmp_alias',
        *(f'test_observed_identity_does_not_accept_replaced_or_later_modified_marker[{defect}]'
          for defect in ('later_edit', 'replacement', 'birth_mismatch', 'call_after_birth',
                         'result_before_modify', 'ctime', 'missing_birth', 'saved_payload', 'observed_before_result')),
    )
} | {_NATIVE_STAGING}


def pytest_addoption(parser):
    group = parser.getgroup('CI evidence')
    group.addoption('--ci-evidence-file', type=Path)
    group.addoption('--ci-expected-file', type=Path)
    group.addoption('--ci-budget-seconds', type=float, default=170)
    group.addoption('--ci-shard', type=int, choices=SHARDS)


def shard_nodes(nodes, shard):
    if shard is None:
        return nodes
    return [node for node in nodes if hashlib.sha256(node.encode()).digest()[0] % len(SHARDS) == shard]


def collection_digest(nodes):
    return hashlib.sha256(json.dumps(nodes, ensure_ascii=False).encode()).hexdigest()


def pytest_collection_modifyitems(config, items):
    shard = config.getoption('ci_shard')
    if shard is None:
        return
    reference = config.getoption('ci_expected_file')
    if config.option.collectonly or reference is None:
        raise pytest.UsageError('ci_shard_requires_full_independent_collection')
    expected = json.loads(reference.read_text())
    if [item.nodeid for item in items] != expected['nodeids']:
        raise pytest.UsageError('ci_independent_collection_mismatch')
    selected = set(shard_nodes(expected['nodeids'], shard))
    deselected = [item for item in items if item.nodeid not in selected]
    items[:] = [item for item in items if item.nodeid in selected]
    config.hook.pytest_deselected(items=deselected)


def pytest_configure(config):
    if not hasattr(config, 'workerinput'):
        config.pluginmanager.register(Evidence(config), 'offline-ci-evidence')


class Evidence:
    def __init__(self, config):
        self.config = config
        self.path = config.getoption('ci_evidence_file')
        self.failure_path = (
            self.path.with_name(self.path.name + '.failures.jsonl') if self.path is not None
            else config.cache.mkdir('failure-records') / f'{secrets.token_hex(16)}.jsonl'
        )
        self.started = time.monotonic()
        self.wall_started = time.time()
        self.cpu_started = time.process_time()
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        self.child_cpu_started = usage.ru_utime + usage.ru_stime
        self.collections = {}
        self.outcomes = defaultdict(list)
        self.durations = defaultdict(float)
        self.workers = defaultdict(lambda: {'collected': 0, 'executed': 0, 'test_seconds': 0.0})
        self.errors = []
        self.expected = None
        self.job_started = os.environ.get('OA_CI_STARTED_AT')
        if (self.path is not None and self.path.exists()) or self.failure_path.exists():
            raise pytest.UsageError('ci_evidence_already_exists')
        if self.path is not None and not config.option.collectonly:
            reference = config.getoption('ci_expected_file')
            if reference is None:
                raise pytest.UsageError('ci_independent_collection_required')
            self.expected = json.loads(reference.read_text())
            if (self.expected.get('status') != 'collected'
                    or self.expected.get('root') != str(config.rootpath)
                    or self.expected.get('collection_sha256') != collection_digest(self.expected.get('nodeids', []))):
                raise pytest.UsageError('ci_collection_untrusted')

    def pytest_collection_finish(self, session):
        if not self.config.getoption('numprocesses', default=0):
            self.collections['main'] = [item.nodeid for item in session.items]

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        self.collections[node.gateway.id] = ids
        self.workers[node.gateway.id]['collected'] = len(ids)

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        if error:
            self.errors.append(f'worker_lost:{node.gateway.id}')

    def pytest_runtest_logreport(self, report):
        worker = getattr(report, 'worker_id', 'main')
        self.durations[report.nodeid] += report.duration
        self.workers[worker]['test_seconds'] += report.duration
        if report.when == 'call' or (report.when == 'setup' and report.outcome != 'passed'):
            self.outcomes[report.nodeid].append(report.outcome)
            self.workers[worker]['executed'] += 1
        if report.failed:
            self.errors.append(f'test_failed:{report.nodeid}:{report.when}')
            self.failure_path.parent.mkdir(parents=True, exist_ok=True)
            failure = {
                'schema_version': 1, 'kind': 'offline_pytest_failure',
                'nodeid': report.nodeid, 'phase': report.when, 'worker': worker,
                'collection_sha256': (self.expected['collection_sha256'] if self.expected is not None
                                      else collection_digest(next(iter(self.collections.values()), []))),
                'code_revision': os.environ.get('GITHUB_SHA'),
                'run_id': os.environ.get('GITHUB_RUN_ID'),
                'run_attempt': int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')),
                'job': os.environ.get('GITHUB_JOB'),
            }
            with self.failure_path.open('a', encoding='utf-8') as file:
                file.write(json.dumps(failure, ensure_ascii=False) + '\n')
                file.flush()
                os.fsync(file.fileno())

    def pytest_sessionfinish(self, session, exitstatus):
        if self.path is None:
            return
        nodes = next(iter(self.collections.values()), [])
        if not nodes or len(nodes) != len(set(nodes)):
            self.errors.append('empty_or_duplicate_collection')
        if any(items != nodes for items in self.collections.values()):
            self.errors.append('worker_collection_mismatch')
        collecting = self.config.option.collectonly
        expected_workers = self.config.getoption('numprocesses', default=0) or 1
        if len(self.collections) != expected_workers:
            self.errors.append('worker_collection_incomplete')
        digest = collection_digest(nodes)
        if not collecting:
            expected_nodes = shard_nodes(self.expected['nodeids'], self.config.getoption('ci_shard'))
            if nodes != expected_nodes or digest != collection_digest(expected_nodes):
                self.errors.append('independent_collection_mismatch')
            if set(self.outcomes) != set(nodes) or any(len(values) != 1 for values in self.outcomes.values()):
                self.errors.append('execution_missing_or_duplicate')
            allowed = _LINUX_SKIPS if sys.platform == 'linux' else set()
            if any(values == ['skipped'] and node not in allowed for node, values in self.outcomes.items()):
                self.errors.append('unexpected_skip')
        elapsed = time.monotonic() - self.started
        scope = 'pytest_session'
        if self.job_started is not None:
            elapsed = time.time() - float(self.job_started)
            scope = 'job_steps'
        if not collecting and not 0 <= elapsed <= self.config.getoption('ci_budget_seconds'):
            self.errors.append('ci_time_budget_exceeded')
        if exitstatus != pytest.ExitCode.OK:
            self.errors.append(f'pytest_exit:{int(exitstatus)}')
        report = {
            'schema_version': 2, 'kind': 'offline_pytest_evidence', 'root': str(self.config.rootpath),
            'status': 'failed' if self.errors else ('collected' if collecting else 'verified'),
            'nodeids': nodes, 'collection_sha256': digest, 'collected_count': len(nodes),
            'executed_count': sum(len(values) for values in self.outcomes.values()),
            'outcomes': dict(self.outcomes), 'counts': dict(Counter(value for values in self.outcomes.values() for value in values)),
            'durations': dict(self.durations), 'workers': dict(self.workers),
            'elapsed_seconds': elapsed, 'timing_scope': scope,
            'started_at': float(self.job_started) if self.job_started is not None else self.wall_started,
            'shard': self.config.getoption('ci_shard'), 'platform': sys.platform,
            'code_revision': os.environ.get('GITHUB_SHA'), 'run_id': os.environ.get('GITHUB_RUN_ID'),
            'run_attempt': int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')),
            'job': os.environ.get('GITHUB_JOB'),
            'cpu_count': os.cpu_count(),
            'cpu_seconds': time.process_time() - self.cpu_started,
            'child_cpu_seconds': (resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
                                  + resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime
                                  - self.child_cpu_started),
            'budget_seconds': self.config.getoption('ci_budget_seconds'), 'errors': self.errors,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('x', encoding='utf-8') as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
            file.write('\n')
        if self.errors and exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _positive_integer(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]*', value):
        raise ValueError('ci_job_output_invalid')
    return int(value)


def _job_evidence(directory):
    """Use the attempts selected by needs, never search backwards for a passing report."""
    attempt = _positive_integer(os.environ['GITHUB_RUN_ATTEMPT'])
    required = json.loads(os.environ['OA_CI_REQUIRED_RESULTS'])
    if set(required) != {'quality', 'macos-stop-recovery'}:
        raise ValueError('ci_required_jobs_invalid')
    errors = [] if all(job.get('result') == 'success' for job in required.values()) else ['required_job_failed']
    selected, artifacts, paths = {}, {}, {}
    roles = [(str(shard), 'quality', f'_{shard}') for shard in SHARDS]
    for role, job, suffix in [*roles, ('macos', 'macos-stop-recovery', '')]:
        outputs = required[job]['outputs']
        selected[role] = _positive_integer(outputs['attempt' + suffix])
        artifacts[role] = _positive_integer(outputs['artifact' + suffix])
        if selected[role] > attempt:
            raise ValueError('ci_job_attempt_invalid')
        paths[role] = directory / f'offline-test-evidence-{role}-attempt-{selected[role]}'
    if len(set(artifacts.values())) != len(selected):
        raise ValueError('ci_duplicate_artifact')
    for path in directory.glob('offline-test-evidence-*'):
        if path.name in {f'offline-test-evidence-{shard}' for shard in SHARDS}:
            continue
        match = re.fullmatch(r'offline-test-evidence-([0-9]+|macos)-attempt-([1-9][0-9]*)', path.name)
        if not match or match[1] not in selected or int(match[2]) > selected[match[1]]:
            raise ValueError('ci_unexpected_artifact')
    return attempt, selected, artifacts, paths, errors


def _valid_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def macos_evidence(path, budget):
    """Bind the existing no-skip macOS check to its actual job attempt."""
    cases = ElementTree.parse(path).findall('.//testcase')
    started = float(os.environ['OA_CI_STARTED_AT'])
    elapsed = time.time() - started
    errors = []
    if not cases or any(case.find(tag) is not None for case in cases for tag in ('skipped', 'failure', 'error')):
        errors.append('stop_recovery_tests_incomplete')
    if not 0 <= elapsed <= budget:
        errors.append('ci_time_budget_exceeded')
    return {'schema_version': 2, 'kind': 'offline_macos_evidence',
            'status': 'failed' if errors else 'verified', 'code_revision': os.environ['GITHUB_SHA'],
            'run_id': os.environ['GITHUB_RUN_ID'], 'run_attempt': _positive_integer(os.environ['GITHUB_RUN_ATTEMPT']),
            'job': os.environ['GITHUB_JOB'], 'platform': sys.platform, 'started_at': started,
            'elapsed_seconds': elapsed, 'budget_seconds': budget, 'test_count': len(cases), 'errors': errors}


def _timing_jobs(jobs, identity):
    names = {*(f'quality ({shard})' for shard in SHARDS), 'macos-stop-recovery', 'quality-complete'}
    head = os.environ.get('OA_CI_HEAD_SHA')
    if (not head or not identity['run_id'] or not identity['run_attempt']
            or jobs['total_count'] != len(jobs['jobs'])
            or len(jobs['jobs']) != len(names) or {job['name'] for job in jobs['jobs']} != names):
        raise ValueError('job_timing_set_untrusted')
    for job in jobs['jobs']:
        if (str(job['run_id']) != identity['run_id']
                or str(job['run_attempt']) != str(identity['run_attempt']) or job['head_sha'] != head):
            raise ValueError('job_timing_identity_mismatch')
    return jobs['jobs']


def matrix_timing(jobs, identity, selected):
    rows = _timing_jobs(jobs, identity)
    head = os.environ.get('OA_CI_HEAD_SHA')
    observed = time.time()
    intervals, timings = {}, {}
    for job in rows:
        name = job['name']
        current = name == 'quality-complete'
        if (job['status'] != ('in_progress' if current else 'completed')
                or job['conclusion'] != (None if current else 'success')):
            raise ValueError('job_timing_status_untrusted')
        start = datetime.fromisoformat(job['started_at'])
        end = None if current else datetime.fromisoformat(job['completed_at'])
        if start.tzinfo is None or (end is not None and end.tzinfo is None):
            raise ValueError('job_timing_clock_untrusted')
        started = start.timestamp()
        finished = observed if current else end.timestamp()
        if not 0 <= started <= finished <= observed:
            raise ValueError('job_timing_clock_untrusted')
        intervals[name] = (started, finished)
        timings[name] = {'started_at': job['started_at'], 'completed_at': job['completed_at'],
                         'elapsed_seconds': finished - started}
    aggregation_start = intervals['quality-complete'][0]
    if any(end > aggregation_start for name, (_, end) in intervals.items() if name != 'quality-complete'):
        raise ValueError('job_timing_clock_untrusted')
    attempts = {**{f'quality ({shard})': selected[str(shard)] for shard in SHARDS},
                'macos-stop-recovery': selected['macos']}
    current_names = {name for name, attempt in attempts.items() if attempt == identity['run_attempt']}
    reused = {name: end - start for name, (start, end) in intervals.items()
              if name in attempts and name not in current_names}
    upstream = max((end - start for name, (start, end) in intervals.items() if name in current_names), default=0)
    aggregation = observed - aggregation_start
    elapsed = upstream + aggregation
    wall = observed - min(start for name, (start, _) in intervals.items()
                          if name in current_names or name == 'quality-complete')
    return {'elapsed_seconds': elapsed, 'timing_scope': 'active_critical_path',
            'upstream_seconds': upstream, 'aggregation_seconds': aggregation,
            'reused_job_seconds': reused,
            'matrix_wall_seconds': wall, 'excluded_scheduling_seconds': wall - elapsed,
            'timing_head_sha': head, 'timing_observed_at': observed, 'job_timings': timings}


def merge_evidence(directory, budget, jobs):
    """Join exact job-attempt evidence, retaining successful jobs not selected for rerun."""
    attempt, selected, artifacts, paths, errors = _job_evidence(directory)
    references = [json.loads((paths[str(shard)] / 'collection.json').read_text())
                  for shard in SHARDS]
    reports = [json.loads((paths[str(shard)] / 'execution.json').read_text())
               for shard in SHARDS]
    nodes = references[0]['nodeids']
    digest = collection_digest(nodes)
    outcomes = defaultdict(list)
    identity = {key: references[0].get(key) for key in ('root', 'platform', 'code_revision', 'run_id')}
    for shard, (reference, report) in enumerate(zip(references, reports)):
        if (reference.get('status') != 'collected' or reference.get('nodeids') != nodes
                or reference.get('collection_sha256') != digest):
            errors.append('full_collection_mismatch')
        if any({key: value.get(key) for key in identity} != identity for value in (reference, report)):
            errors.append('evidence_identity_mismatch')
        if any(value.get('schema_version') != 2 or value.get('job') != 'quality'
                or type(value.get('run_attempt')) is not int or value['run_attempt'] != selected[str(shard)]
                for value in (reference, report)):
            errors.append('evidence_attempt_mismatch')
        if (os.environ.get('GITHUB_SHA') != identity['code_revision']
                or os.environ.get('GITHUB_RUN_ID') != identity['run_id']):
            errors.append('evidence_run_mismatch')
        if (not _valid_time(reference.get('started_at')) or reference['started_at'] != report.get('started_at')
                or not _valid_time(report.get('elapsed_seconds')) or report['elapsed_seconds'] > budget):
            errors.append('evidence_timing_invalid')
        expected = shard_nodes(nodes, shard)
        if (report.get('shard') != shard or report.get('status') != 'verified' or report.get('errors')
                or report.get('nodeids') != expected
                or report.get('collection_sha256') != collection_digest(expected)
                or report.get('collected_count') != len(expected)
                or report.get('executed_count') != len(expected)
                or set(report.get('outcomes', {})) != set(expected)):
            errors.append(f'shard_incomplete:{shard}')
        for node, values in report.get('outcomes', {}).items():
            outcomes[node].extend(values)
    if (not nodes or len(nodes) != len(set(nodes)) or set(outcomes) != set(nodes)
            or any(values not in (['passed'], ['skipped']) for values in outcomes.values())):
        errors.append('execution_missing_or_duplicate')
    allowed = _LINUX_SKIPS if identity['platform'] == 'linux' else set()
    if any(values == ['skipped'] and node not in allowed for node, values in outcomes.items()):
        errors.append('unexpected_skip')
    macos_nodes = [node.split('::')[-1] for node in nodes
                   if node.startswith(_MANUAL_STOP) or node == _NATIVE_STAGING]
    macos = json.loads((paths['macos'] / 'macos.json').read_text())
    if (macos.get('schema_version') != 2 or macos.get('job') != 'macos-stop-recovery'
            or macos.get('platform') != 'darwin' or type(macos.get('run_attempt')) is not int
            or macos['run_attempt'] != selected['macos']
            or any(macos.get(key) != identity[key] for key in ('code_revision', 'run_id'))):
        errors.append('macos_identity_mismatch')
    cases = ElementTree.parse(paths['macos'] / 'stop-recovery.xml').findall('.//testcase')
    if (macos.get('status') != 'verified' or macos.get('errors')
            or Counter(case.get('name') for case in cases) != Counter(macos_nodes)
            or any(case.find(tag) is not None for case in cases for tag in ('skipped', 'failure', 'error'))):
        errors.append('macos_stop_recovery_incomplete')
    if (not _valid_time(macos.get('started_at')) or not _valid_time(macos.get('elapsed_seconds'))
            or macos['elapsed_seconds'] > budget):
        errors.append('evidence_timing_invalid')
    timing = {'elapsed_seconds': None, 'timing_scope': 'active_critical_path'}
    try:
        timing = matrix_timing(jobs, {**identity, 'run_attempt': attempt}, selected)
    except (KeyError, TypeError, ValueError) as error:
        errors.append(str(error) if str(error).startswith('job_timing_') else 'job_timing_untrusted')
    if (timing['elapsed_seconds'] is not None
            and (not 0 <= timing['elapsed_seconds'] <= budget
                 or any(seconds > budget for seconds in timing['reused_job_seconds'].values()))):
        errors.append('ci_time_budget_exceeded')
    roles = {**{str(shard): f'quality-{shard}' for shard in SHARDS}, 'macos': 'macos-stop-recovery'}
    return {'schema_version': 2, 'kind': 'offline_pytest_matrix_evidence',
            'status': 'failed' if errors else 'verified', **identity,
            'run_attempt': attempt, 'evidence_attempts': {roles[role]: value for role, value in selected.items()},
            'artifact_ids': {roles[role]: value for role, value in artifacts.items()},
            'reused_jobs': [roles[role] for role, value in selected.items() if value < attempt],
            'nodeids': nodes, 'collection_sha256': digest, 'collected_count': len(nodes),
            'executed_count': sum(len(values) for values in outcomes.values()),
            'counts': dict(Counter(value for values in outcomes.values() for value in values)),
            'macos_stop_recovery_count': sum(node.startswith(_MANUAL_STOP) for node in nodes),
            'macos_native_staging_count': int(_NATIVE_STAGING in nodes), 'shards': reports,
            **timing,
            'budget_seconds': budget, 'errors': errors}


def _pending_job_timings(jobs, identity):
    rows = _timing_jobs(jobs, identity)
    observed = time.time()
    aggregation = next(job for job in rows if job['name'] == 'quality-complete')
    aggregation_start = datetime.fromisoformat(aggregation['started_at'])
    if aggregation_start.tzinfo is None or not 0 <= aggregation_start.timestamp() <= observed:
        raise ValueError('job_timing_clock_untrusted')
    pending = False
    for job in rows:
        current = job['name'] == 'quality-complete'
        unfinished = job['status'] in {'queued', 'in_progress'} and job['conclusion'] is None
        if current or unfinished:
            if not unfinished or job['completed_at'] is not None:
                raise ValueError('job_timing_status_untrusted')
            pending = pending or not current or job['status'] == 'queued'
        elif job['status'] != 'completed' or job['conclusion'] != 'success':
            raise ValueError('job_timing_status_untrusted')
        start = datetime.fromisoformat(job['started_at'])
        end = start if unfinished else datetime.fromisoformat(job['completed_at'])
        if (start.tzinfo is None or end.tzinfo is None
                or not 0 <= start.timestamp() <= end.timestamp() <= aggregation_start.timestamp()):
            raise ValueError('job_timing_clock_untrusted')
    return pending


def fetch_job_timings(path):
    identity = {'run_id': os.environ['GITHUB_RUN_ID'], 'run_attempt': os.environ['GITHUB_RUN_ATTEMPT']}
    repository = os.environ['GITHUB_REPOSITORY']
    endpoint = (f'repos/{repository}/actions/runs/{identity["run_id"]}'
                f'/attempts/{identity["run_attempt"]}/jobs?per_page=100')
    snapshots = path.with_suffix('.snapshots')
    snapshots.mkdir()
    for read in range(1, 21):
        response = subprocess.run(['gh', 'api', endpoint], capture_output=True, timeout=30)
        with (snapshots / f'{read:02}.json').open('xb') as file:
            file.write(response.stdout)
        try:
            if response.returncode:
                raise ValueError('job_timing_fetch_failed')
            jobs = json.loads(response.stdout)
            pending = _pending_job_timings(jobs, identity)
        except (KeyError, TypeError, ValueError):
            with path.open('xb') as file:
                file.write(response.stdout)
            raise
        if not pending or read == 20:
            with path.open('xb') as file:
                file.write(response.stdout)
            if pending:
                raise ValueError('job_timing_readiness_exhausted')
            return jobs
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description='Verify complete offline CI evidence')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--merge-directory', type=Path)
    mode.add_argument('--macos-report', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs-file', type=Path)
    parser.add_argument('--fetch-jobs', action='store_true')
    parser.add_argument('--budget-seconds', type=float, default=170)
    args = parser.parse_args()
    if args.merge_directory and args.jobs_file is None:
        parser.error('--merge-directory requires --jobs-file')
    if args.fetch_jobs and not args.merge_directory:
        parser.error('--fetch-jobs requires --merge-directory')
    try:
        jobs = (fetch_job_timings(args.jobs_file) if args.fetch_jobs else
                json.loads(args.jobs_file.read_text()) if args.merge_directory else None)
        report = (merge_evidence(args.merge_directory, args.budget_seconds, jobs)
                  if args.merge_directory
                  else macos_evidence(args.macos_report, args.budget_seconds))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, ElementTree.ParseError) as error:
        report = {'status': 'failed', 'counts': {}, 'elapsed_seconds': None,
                  'errors': [str(error) if type(error) is ValueError else f'evidence_input_invalid:{type(error).__name__}']}
    with args.output.open('x', encoding='utf-8') as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in ('status', 'counts', 'elapsed_seconds', 'errors') if key in report}))
    return int(report['status'] != 'verified')


if __name__ == '__main__':
    raise SystemExit(main())
