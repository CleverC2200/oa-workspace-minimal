"""Opt-in pytest evidence: independent collection, exact execution and time budget."""
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import sys
import time

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


def pytest_configure(config):
    if config.getoption('ci_evidence_file') and not hasattr(config, 'workerinput'):
        config.pluginmanager.register(Evidence(config), 'offline-ci-evidence')


class Evidence:
    def __init__(self, config):
        self.config = config
        self.path = config.getoption('ci_evidence_file')
        self.started = time.monotonic()
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
                    or self.expected.get('root') != str(config.rootpath)):
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
        digest = hashlib.sha256(json.dumps(nodes, ensure_ascii=False).encode()).hexdigest()
        if not collecting:
            if nodes != self.expected['nodeids'] or digest != self.expected['collection_sha256']:
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
            'budget_seconds': self.config.getoption('ci_budget_seconds'), 'errors': self.errors,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('x', encoding='utf-8') as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
            file.write('\n')
        if self.errors and exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
