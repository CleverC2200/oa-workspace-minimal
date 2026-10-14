"""Opt-in pytest evidence: independent collection, exact execution and time budget."""
from collections import Counter, defaultdict
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import time
from xml.etree import ElementTree

import pytest


_MANUAL_STOP = 'tests/finance_preaudit/test_manual_stop_recovery.py::'
_LINUX_SKIPS = {
    _MANUAL_STOP + name for name in (
        'test_first_maintenance_identity_is_bound_to_original_file_birth',
        'test_original_evidence_accepts_system_tmp_alias_without_changing_literal_binding',
        'test_private_evidence_rejects_untrusted_tmp_alias',
        *(f'test_observed_identity_does_not_accept_replaced_or_later_modified_marker[{defect}]'
          for defect in ('later_edit', 'replacement', 'birth_mismatch', 'call_after_birth',
                         'result_before_modify', 'ctime', 'missing_birth', 'saved_payload', 'observed_before_result')),
    )
}


def pytest_addoption(parser):
    group = parser.getgroup('CI evidence')
    group.addoption('--ci-evidence-file', type=Path)
    group.addoption('--ci-expected-file', type=Path)
    group.addoption('--ci-budget-seconds', type=float, default=170)
    group.addoption('--ci-shard', type=int, choices=(0, 1))


def shard_nodes(nodes, shard):
    if shard is None:
        return nodes
    return [node for node in nodes if hashlib.sha256(node.encode()).digest()[0] % 2 == shard]


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
    if config.getoption('ci_evidence_file') and not hasattr(config, 'workerinput'):
        config.pluginmanager.register(Evidence(config), 'offline-ci-evidence')


class Evidence:
    def __init__(self, config):
        self.config = config
        self.path = config.getoption('ci_evidence_file')
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
        if self.path.exists():
            raise pytest.UsageError('ci_evidence_already_exists')
        if not config.option.collectonly:
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

    def pytest_sessionfinish(self, session, exitstatus):
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
            'schema_version': 1, 'kind': 'offline_pytest_evidence', 'root': str(self.config.rootpath),
            'status': 'failed' if self.errors else ('collected' if collecting else 'verified'),
            'nodeids': nodes, 'collection_sha256': digest, 'collected_count': len(nodes),
            'executed_count': sum(len(values) for values in self.outcomes.values()),
            'outcomes': dict(self.outcomes), 'counts': dict(Counter(value for values in self.outcomes.values() for value in values)),
            'durations': dict(self.durations), 'workers': dict(self.workers),
            'elapsed_seconds': elapsed, 'timing_scope': scope,
            'started_at': float(self.job_started) if self.job_started is not None else self.wall_started,
            'shard': self.config.getoption('ci_shard'), 'platform': sys.platform,
            'code_revision': os.environ.get('GITHUB_SHA'), 'run_id': os.environ.get('GITHUB_RUN_ID'),
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


def merge_evidence(directory, budget):
    """Join the fixed two-runner matrix against both independent full collections."""
    references = [json.loads((directory / f'offline-test-evidence-{shard}' / 'collection.json').read_text())
                  for shard in (0, 1)]
    reports = [json.loads((directory / f'offline-test-evidence-{shard}' / 'execution.json').read_text())
               for shard in (0, 1)]
    nodes = references[0]['nodeids']
    digest = collection_digest(nodes)
    errors = []
    required = os.environ.get('OA_CI_REQUIRED_RESULTS')
    if required is not None:
        required = json.loads(required)
        if (set(required) != {'quality', 'macos-stop-recovery'}
                or any(job.get('result') != 'success' for job in required.values())):
            errors.append('required_job_failed')
    if {path.name for path in directory.glob('offline-test-evidence-*')} != {
            'offline-test-evidence-0', 'offline-test-evidence-1'}:
        errors.append('shard_set_incomplete')
    outcomes = defaultdict(list)
    identity = {key: references[0].get(key) for key in ('root', 'platform', 'code_revision', 'run_id')}
    for shard, (reference, report) in enumerate(zip(references, reports)):
        if (reference.get('status') != 'collected' or reference.get('nodeids') != nodes
                or reference.get('collection_sha256') != digest):
            errors.append('full_collection_mismatch')
        if any({key: value.get(key) for key in identity} != identity for value in (reference, report)):
            errors.append('evidence_identity_mismatch')
        if (os.environ.get('GITHUB_SHA') != identity['code_revision']
                or os.environ.get('GITHUB_RUN_ID') != identity['run_id']):
            errors.append('evidence_run_mismatch')
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
    manual_nodes = [node.split('::')[-1] for node in nodes if node.startswith(_MANUAL_STOP)]
    macos_files = list(directory.rglob('stop-recovery.xml'))
    starts = [reference['started_at'] for reference in references]
    if manual_nodes or macos_files:
        cases = ElementTree.parse(macos_files[0]).findall('.//testcase') if len(macos_files) == 1 else []
        if (Counter(case.get('name') for case in cases) != Counter(manual_nodes)
                or any(case.find(tag) is not None for case in cases for tag in ('skipped', 'failure', 'error'))):
            errors.append('macos_stop_recovery_incomplete')
        macos_starts = list(directory.rglob('stop-recovery-started-at.txt'))
        if len(macos_starts) != 1:
            errors.append('macos_start_evidence_missing')
        else:
            starts.append(float(macos_starts[0].read_text()))
    elapsed = time.time() - min(starts)
    if not 0 <= elapsed <= budget:
        errors.append('ci_time_budget_exceeded')
    return {'schema_version': 1, 'kind': 'offline_pytest_matrix_evidence',
            'status': 'failed' if errors else 'verified', **identity,
            'nodeids': nodes, 'collection_sha256': digest, 'collected_count': len(nodes),
            'executed_count': sum(len(values) for values in outcomes.values()),
            'counts': dict(Counter(value for values in outcomes.values() for value in values)),
            'macos_stop_recovery_count': len(manual_nodes), 'shards': reports,
            'elapsed_seconds': elapsed, 'timing_scope': 'matrix_and_aggregation',
            'budget_seconds': budget, 'errors': errors}


def main():
    parser = argparse.ArgumentParser(description='Verify complete two-runner offline CI evidence')
    parser.add_argument('--merge-directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--budget-seconds', type=float, default=170)
    args = parser.parse_args()
    report = merge_evidence(args.merge_directory, args.budget_seconds)
    with args.output.open('x', encoding='utf-8') as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in ('status', 'counts', 'elapsed_seconds', 'errors')}))
    return int(report['status'] != 'verified')


if __name__ == '__main__':
    raise SystemExit(main())
