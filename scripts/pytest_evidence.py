"""Local pytest failure retention and explicit complete execution evidence."""
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import resource
import secrets
import sys
import time

import pytest


def pytest_addoption(parser):
    group = parser.getgroup('execution evidence')
    group.addoption('--ci-evidence-file', type=Path)
    group.addoption('--ci-expected-file', type=Path)


def collection_digest(nodes):
    return hashlib.sha256(json.dumps(nodes, ensure_ascii=False).encode()).hexdigest()


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
            expected_nodes = self.expected['nodeids']
            if nodes != expected_nodes or digest != collection_digest(expected_nodes):
                self.errors.append('independent_collection_mismatch')
            if set(self.outcomes) != set(nodes) or any(len(values) != 1 for values in self.outcomes.values()):
                self.errors.append('execution_missing_or_duplicate')
            if any(values == ['skipped'] for values in self.outcomes.values()):
                self.errors.append('unexpected_skip')
        elapsed = time.monotonic() - self.started
        if exitstatus != pytest.ExitCode.OK:
            self.errors.append(f'pytest_exit:{int(exitstatus)}')
        report = {
            'schema_version': 2, 'kind': 'offline_pytest_evidence', 'root': str(self.config.rootpath),
            'status': 'failed' if self.errors else ('collected' if collecting else 'verified'),
            'nodeids': nodes, 'collection_sha256': digest, 'collected_count': len(nodes),
            'executed_count': sum(len(values) for values in self.outcomes.values()),
            'outcomes': dict(self.outcomes), 'counts': dict(Counter(value for values in self.outcomes.values() for value in values)),
            'durations': dict(self.durations), 'workers': dict(self.workers),
            'elapsed_seconds': elapsed, 'timing_scope': 'pytest_session',
            'started_at': self.wall_started, 'platform': sys.platform,
            'code_revision': os.environ.get('GITHUB_SHA'), 'run_id': os.environ.get('GITHUB_RUN_ID'),
            'run_attempt': int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')),
            'job': os.environ.get('GITHUB_JOB'),
            'cpu_count': os.cpu_count(),
            'cpu_seconds': time.process_time() - self.cpu_started,
            'child_cpu_seconds': (resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
                                  + resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime
                                  - self.child_cpu_started),
            'errors': self.errors,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('x', encoding='utf-8') as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
            file.write('\n')
        if self.errors and exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
