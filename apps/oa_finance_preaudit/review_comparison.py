"""Private actual-version coordination over the original paired plans.

The public effect protocol is unchanged. The caller owns the original parent
lock, source qualification and authority; only selected unconsumed sides cross
the existing source host. This module never loads credentials or writes OA.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable
from datetime import datetime, timezone
import copy
import importlib.util
import os
import subprocess
import time

from . import flow_evals, review_batch_report as batch, review_experiment as e, review_references

ARMS = ('baseline', 'candidate')


def _ref(path):
    return {'file': str(Path(path).absolute()), 'sha256': batch._sha(Path(path))}


def _read(reference):
    from .flow_evals import _read_bound_artifact
    return _read_bound_artifact(reference)


def _need(value, reason):
    if not value:
        raise ValueError(reason)


def _unowned(plan, arm):
    root = Path(plan['arms'][arm])
    _need(not any((root / name).exists() for name in
                  ('started.json', 'cli-start.json', 'reviewer', 'result.json')),
          'pending_arm_already_owned')


def _terminal(plan, arm, report):
    root = Path(plan['arms'][arm])
    raw = batch._object(root, 'result.json')
    _need(e._observed_cli_start(plan, arm) == 1
          and report['source']['revision'] == plan['source_revisions'][arm]
          and report['source']['archived_bytes'] == 'verified'
          and report['source']['role']['status'] == 'bound_runtime_record'
          and report['source']['role']['declared'] == plan['source_roles'][arm]
          and report['write_authorization'] is False
          and raw['write_state'] == 'not_requested', 'comparison_result_untrusted')
    process = report['acceptance']['process']
    _need(process['state'] == 'exited' and process['exit_code'] == 0
          and report['usage']['status'] == 'exact', 'comparison_process_or_usage_unverified')
    _need(report['acceptance']['preview'] == 'verified_closed'
          and report['acceptance']['final_contract'] == 'verified_snapshot_schema'
          and report['financial_binding']['status'] == 'verified_consumed_correspondence',
          'comparison_output_rejected')


def _execute_pending_pair(plan_file: Path, selection: dict, authority: dict, *,
                          revalidate: Callable[[], None],
                          stop_requested: Callable[[], bool] = lambda: False) -> dict:
    """Caller-internal transport seam. Caller holds the parent lock throughout.

    revalidate rereads bound scope/authority and counts original receipts before
    each side. A completed transport is awaiting independent financial assessment.
    No callback may replace native execution or its actual archive report.
    """
    from .flow_evals import _read_bound_artifact
    plan_file = Path(plan_file)
    plan = _read_bound_artifact(selection['target_plan_ref'])
    _need(selection['target_plan_ref']['file'] == str(plan_file), 'target_plan_binding_invalid')
    pending = selection['pending_arms']
    _need(isinstance(pending, list) and pending and len(pending) == len(set(pending))
          and set(pending) <= {'baseline', 'candidate'}
          and set(pending).isdisjoint(selection['reused_originals']), 'duplicate_or_unknown_side')
    reports, raw_results, dispatched = {}, {}, []
    starts = 0
    dispatching = None
    result_file = plan_file.parent / 'comparison-execution.private.json'
    claim_file = plan_file.with_name(plan_file.name + '.started.json')
    try:
        _need(not result_file.exists(), 'pair_already_observed_no_retry')
        if claim_file.exists():
            _need(bool(selection['reused_originals'])
                  and batch._object(claim_file.parent, claim_file)['plan_sha256'] == selection['target_plan_ref']['sha256'],
                  'pair_already_owned')
        for arm in pending:
            revalidate()
            _valid = _read_bound_artifact(selection['target_plan_ref'])
            _need(_valid == plan, 'target_plan_binding_invalid')
            e._valid_authority_time(authority)
            _need(not stop_requested(), 'cancelled_before_dispatch')
            _unowned(plan, arm)
            manifest = e._verify_sources(Path(plan['arms'][arm]), Path(plan['repository']))
            _need(manifest['source_revision'] == plan['source_revisions'][arm]
                  and batch._sha(Path(plan['arms'][arm]) / 'manifest.json') == plan['manifests'][arm],
                  'source_drift')
            current = e._host(plan_file, arm)
            _need(all(current[key] == plan['preparations'][arm][key] for key in
                      ('conditions', 'delivery', 'input_sha256', 'source_snapshot_sha256')),
                  'prepared_input_drift')
            revalidate()
            e._valid_authority_time(authority)
            _need(not stop_requested(), 'cancelled_before_dispatch')
            if not claim_file.exists():
                e._save(claim_file, {'plan_sha256': selection['target_plan_ref']['sha256'],
                                    'purpose': 'explicit_actual_version_comparison',
                                    'pending_arms': pending, 'no_retry': True})
            dispatching = arm
            output = e._host(plan_file, arm, execute=True)
            raw_results[arm] = output
            observed = e._observed_cli_start(plan, arm)
            _need(type(output.get('cli_starts')) is int and output['cli_starts'] == observed == 1,
                  'start_count_unknown')
            starts += observed
            dispatched.append(arm)
            # Keep ownership until both the actual report and its start agree.
            report = batch.build_report(Path(plan['arms'][arm]))
            reports[arm] = report
            dispatching = None
            _need(not output.get('dispatch_cancelled')
                  and output['execution']['process_state'] != 'cancelled',
                  'cancelled_during_review')
            _terminal(plan, arm, report)
        result = dict(status='awaiting_independent_assessment', new_cli_starts=starts,
                      reports=reports, raw_results=raw_results, dispatched_arms=dispatched, not_started=[],
                      financial_qualification=False, benefit='not_established', oa_writes=0)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        if dispatching is not None:
            observed = e._observed_cli_start(plan, dispatching)
            # A report failure after a verified start must not count it twice.
            if dispatching not in dispatched:
                starts = 'unknown' if observed == 'unknown' else starts + observed
                dispatched.append(dispatching)
        result = dict(status='stopped' if dispatched else 'rejected', reason=str(error),
                      new_cli_starts=starts, reports=reports, raw_results=raw_results, dispatched_arms=dispatched,
                      not_started=[arm for arm in pending if arm not in dispatched],
                      financial_qualification=False, benefit='not_established', oa_writes=0)
    if not result_file.exists():
        try:
            e._save(result_file, result)
        except (OSError, ValueError):
            result.update(status='stopped', reason='comparison_result_persistence_failed')
    return result


def _side_count(path, plan, arm):
    root = Path(plan['arms'][arm])
    owned = any((root / name).exists() for name in ('started.json', 'cli-start.json', 'reviewer', 'result.json'))
    if not owned:
        return 0
    value = e._observed_cli_start(plan, arm)
    _need(type(value) is int and value == 1, 'start_count_unknown')
    return value

def _pair_rows(scope):
    return [(row, Path(row['plan']['file']), _read(row['plan'])) for row in scope['cases']]

def _counted_sides(scope):
    """Original plan and side identities, never candidate renaming or copied results."""
    found = set()
    for row, path, plan in _pair_rows(scope):
        reused = row.get('reuse', {})
        counts = []
        for arm in ARMS:
            if arm in reused:
                original = _read(reused[arm]['plan'])
                original_arm = reused[arm]['arm']
                root = Path(plan['arms'][arm])
                owned = any((root / name).exists() for name in
                            ('started.json', 'cli-start.json', 'reviewer', 'result.json'))
                _need(not owned or root == Path(original['arms'][original_arm]),
                      'reused_side_has_new_consumption')
                counts.append(0)
            else:
                counts.append(_side_count(path, plan, arm))
        if not any(counts) and not reused and path.with_name(path.name + '.started.json').exists():
            raise ValueError('start_count_unknown')
        for arm, count in zip(ARMS, counts):
            if count:
                found.add((str(path), batch._sha(path), arm))
    return found

def _parent_budget(authority, scope, scope_reference, *, verified=None):
    """Project the existing parent chain into genuine plan/arm starts.

    The candidate-only public budget cannot count baseline. This private final
    projection reads the same chain and original receipts; it owns no ledger.
    """
    parent = _read(authority['parent_cumulative_authority'])
    _pointer, chain = e._candidate_parent_chain(authority)
    current_ref = scope_reference
    _need(scope == _read(current_ref) and authority['scope_sha256'] == current_ref['sha256'], 'phase_scope_binding_invalid')
    current = chain[0]
    if current['scope'] == current_ref:
        _need(authority == _read(current['authority']), 'phase_authority_drift')
    else:
        _need(authority['prior_phase_authority'] == current['authority']
             and authority['prior_phase_scope'] == current['scope'], 'prior_phase_binding_required')
    anchors = e._candidate_parent_anchor_plans(parent)
    original = chain[-1]['original_started_plans']
    required = {(r['file'], r['sha256']) for r in original}
    historical = set()
    diagnostic = set()
    for r in original:
        plan = _read(r)
        _need(e._candidate_phase_start(Path(r['file']), plan) == 1, 'prior_start_untrusted')
        historical.add((r['file'], r['sha256'], 'candidate'))
        if (r['file'], r['sha256']) not in anchors:
            diagnostic.add((r['file'], r['sha256'], 'candidate'))
    for node in chain:
        if node['scope'] == current_ref:
            continue
        prior_scope = _read(node['scope'])
        prior_authority = _read(node['authority'])
        if prior_authority.get('phase') == 'final_production_code_comparison':
            starts = _counted_sides(prior_scope)
        else:
            plans = e._candidate_phase_started_plans(prior_scope)
            starts = {(file, digest, 'candidate') for file, digest in plans}
            for _row, _path, plan in _pair_rows(prior_scope):
                _need(_side_count(_path, plan, 'baseline') == 0, 'baseline_execution_forbidden')
            diagnostic.update(start for start in starts if start[:2] not in anchors)
        historical.update(starts)
        required.update((file, digest) for file, digest, _arm in starts)
    current_paths = {r['plan']['file'] for r in scope['cases']}
    _need(not current_paths.intersection(file for file, _digest, _arm in historical),
         'target_plan_overlaps_parent_history')
    _need({(r['file'], r['sha256']) for r in authority['prior_plans']} == required
         and len(authority['prior_plans']) == len(required), 'prior_budget_scope_invalid')
    for row in scope['cases']:
        for refs in row.get('reuse', {}).values():
            _need((refs['plan']['file'], refs['plan']['sha256'], refs['arm']) in historical,
                  'reused_original_not_counted_by_parent')
    verified = verified or batch._VerifiedReports()
    for file, digest, arm in historical:
        plan = _read({'file': file, 'sha256': digest})
        report = verified.read(Path(plan['arms'][arm]))
        process = report['acceptance']['process']
        _need(report['usage']['status'] == 'exact'
             and process['state'] in {'exited', 'cancelled', 'timed_out'}
             and type(process.get('exit_code')) is int
             and report['source']['revision'] == plan['source_revisions'][arm]
             and report['source']['archived_bytes'] == 'verified'
             and report['source']['role']['status'] == 'bound_runtime_record'
             and report['write_authorization'] is False, 'prior_execution_untrusted')
        raw = _read(_ref(Path(plan['arms'][arm]) / 'result.json'))
        _need(raw['write_state'] == 'not_requested', 'prior_write_state_unknown')
    starts = _counted_sides(scope)
    for file, digest, arm in starts:
        plan = _read({'file': file, 'sha256': digest})
        report = verified.read(Path(plan['arms'][arm]))
        process = report['acceptance']['process']
        _need(report['usage']['status'] == 'exact'
              and process['state'] in {'exited', 'cancelled', 'timed_out'}
              and type(process.get('exit_code')) is int
              and report['source']['revision'] == plan['source_revisions'][arm]
              and report['source']['archived_bytes'] == 'verified'
              and report['source']['role']['status'] == 'bound_runtime_record'
              and report['write_authorization'] is False, 'current_execution_untrusted')
        raw = _read(_ref(Path(plan['arms'][arm]) / 'result.json'))
        _need(raw['write_state'] == 'not_requested', 'current_write_state_unknown')
    if authority.get('phase') != 'final_production_code_comparison':
        diagnostic.update(start for start in starts if start[2] == 'candidate' and start[:2] not in anchors)
    return {'phase_cli_starts': len(starts), 'global_cli_starts': len(historical | starts),
            'diagnostic_cli_starts': len(diagnostic), 'parent_ref': authority['parent_cumulative_authority']}


class _ComparisonCaller:
    """Task-private paired caller; fixed immutable configuration, one run entry."""

    def __init__(self, *, caller_file: Path, identity_bank: dict, adapter_file: dict,
                 reference_module: dict | None = None):
        self.caller_file = Path(caller_file)
        self.identity_bank = identity_bank
        self._module_ref = _ref(Path(__file__))
        self._adapter_ref = copy.deepcopy(adapter_file)
        self._reference_ref = reference_module or _ref(Path(review_references.__file__))
        _need(self._reference_ref['file'] == str(Path(review_references.__file__).resolve()),
              'reference_module_not_bound')
        from .flow_evals import _read_bound_artifact
        _read_bound_artifact(adapter_file, document=False)
        spec = importlib.util.spec_from_file_location("private_reference_adapter", adapter_file["file"])
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        self.adapter = adapter

    def _verify_runtime(self):
        from .flow_evals import _read_bound_artifact
        for reference in (self._module_ref, self._adapter_ref, self._reference_ref):
            _read_bound_artifact(reference, document=False)

    def _side_expectations(self, row, plan, arm):
        """Every final side has its original independent, pre-run four facts."""
        checklist_ref = row['checklist' if arm == 'candidate' else 'baseline_checklist']
        freeze_ref = row['expectations' if arm == 'candidate' else 'baseline_expectations']
        checklist, freeze = _read(checklist_ref), _read(freeze_ref)
        _need(freeze.get('kind') == 'preaudit_expectations_freeze'
              and freeze.get('checklist_sha256') == checklist_ref['sha256'],
              'expectations_not_bound')
        root = Path(plan['arms'][arm])
        manifest, source_files, _snapshot = review_references.archive_sources(_ref(root / 'manifest.json'))
        packet = batch._object(root, Path(manifest['action_file']).with_name('review-packet.json'))
        checks = checklist['checks']
        _need(checklist.get('source_kind') == 'real_archive'
              and checklist.get('workflow_key') == 'business_entertainment'
              and checklist.get('review_scope') == 'complete_financial_review'
              and len(checks) == len({check['id'] for check in checks}) == 4
              and {check.get('item_key') for check in checks} == {item['item_key'] for item in packet['items']},
              'final_expectations_scope_incomplete')
        for reference in checklist['source_refs'] + checklist['rule_refs'] + checklist.get('history_refs', []):
            flow_evals._read_bound_artifact(reference, document=False)
        docs = ('SKILL.md', 'references/result-contract.md', 'references/observation-contract.md',
                'references/flow-engine-contract.md', 'references/flows/business-entertainment.md')
        rules = [source_files['.agents/skills/oa-finance-preaudit/' + name] for name in docs]
        configs = {Path(name).name: digest for name, digest in source_files.items()
                   if name.startswith('apps/oa_approval/configs/') and name.endswith('.json')}
        _need(sorted(ref['sha256'] for ref in checklist['source_refs']) == sorted(manifest['originals'].values())
              and sorted(ref['sha256'] for ref in checklist['rule_refs']) == sorted(rules)
              and freeze['flow_configs'] == configs
              and freeze['historical_crosscheck'] == flow_evals._historical_crosscheck(checklist),
              'final_expectations_source_mismatch')
        try:
            frozen = datetime.fromisoformat(freeze['frozen_at'])
            _need(frozen.tzinfo is not None and frozen <= datetime.now(timezone.utc),
                  'final_expectations_not_frozen_before_dispatch')
        except (TypeError, ValueError) as error:
            raise ValueError('final_expectations_not_frozen_before_dispatch') from error
        reused = row.get('reuse', {}).get(arm)
        if reused is not None:
            _need(reused['checklist'] == checklist_ref and reused['freeze'] == freeze_ref,
                  'reused_expectations_not_original')
            self.adapter.match_target(row['plan'], arm, reused, row.get('snapshot_audit'))
        elif (root / 'result.json').exists():
            report = batch.build_report(root)
            _need(flow_evals._expectations_timing(freeze, checklist_ref['sha256'],
                                                report['timing']['start_receipt'])['status'] == 'verified',
                  'final_expectations_not_frozen_before_dispatch')
        if freeze.get('history_resolution') is not None:
            _need(freeze.get('schema_version') == 2
                  and freeze.get('qualification_semantics') == flow_evals._HISTORY_SCOPE_SEMANTICS,
                  'financial_history_resolution_invalid')
            flow_evals._history_scope_resolution(freeze['history_resolution'], checklist_ref,
                                                checklist, freeze['frozen_at'])

    def verify_scope(self, scope_ref):
        scope = _read(scope_ref)
        _need(self.adapter.sha(Path(self.identity_bank['file'])) == self.identity_bank['sha256'], 'identity_bank_changed')
        _need(scope['identity_bank'] == {'file': str(Path(self.identity_bank['file'])), 'sha256': self.identity_bank['sha256']}, 'identity_bank_unbound')
        _need(scope['purpose'] == 'actual_version_comparison_not_isolated_input_effect'
             and scope['oa_write_authorized'] is False and scope['automatic_retries'] == 0
             and len(scope['cases']) == 20 and scope['maximum_per_arm'] == 1
             and scope['timeout_seconds_per_arm'] == 900, 'final_scope_invalid')
        bank = _read(scope['identity_bank'])
        bank_rows = bank['cases'] if isinstance(bank, dict) else bank
        identities = {r['alias']: r for r in bank_rows}
        _need(len({r['alias'] for r in scope['cases']}) == 20
              and len({r['stable_identity_sha256'] for r in scope['cases']}) == 20,
              'scope_duplicate_case')
        samples = set()
        for row, path, plan in _pair_rows(scope):
            bank_row = identities[row['alias']]
            _need(row['stable_identity_sha256'] == bank_row['stable_identity_sha256'], 'case_identity_changed')
            original = _read(bank_row['material'])
            _need(plan['source_revisions'] == {'baseline': scope['baseline'], 'candidate': scope['candidate']}
                 and plan['source_roles'] == {'baseline': 'production_code_replay', 'candidate': 'local_candidate'}
                 and plan['oa_write_authorized'] is False and plan['timeout_seconds'] == 900
                 and plan['maximum_cli_starts'] == 2, 'target_plan_contract_invalid')
            _need(path == path.resolve() and not path.is_symlink(), 'target_plan_path_invalid')
            before, after = (plan['preparations'][arm]['conditions'] for arm in ARMS)
            differences = [key for key in before if before[key] != after.get(key)]
            declared = row.get('conditions_differences', row.get('condition_differences', scope.get('condition_differences')))
            _need(set(before) == set(after)
                  and differences == declared
                  and set(differences) <= {'rules', 'call_profile'},
                  'comparison_conditions_mismatch')
            _need(after['sample'] not in samples, 'scope_duplicate_case')
            samples.add(after['sample'])
            for arm in ARMS:
                root = Path(plan['arms'][arm])
                manifest = batch._object(root, 'manifest.json')
                material = batch._object(root, Path(manifest['action_file']).with_name('material.json'))
                _need(material['request_id'] == original['request_id']
                      and material['workflow_key'] == original['workflow_key'] == 'business_entertainment',
                      'case_identity_changed')
                self._side_expectations(row, plan, arm)
        return scope

    def source_quality(self, request, scope, *, verified=None):
        prerequisites = request['prerequisites']
        source_scope = _read(prerequisites['source_scope'])
        source_authority = _read(prerequisites['source_authority'])
        source_closed = _read(prerequisites['source_closed'])
        final_authority = _read(request['authority'])
        _pointer, parent_chain = e._candidate_parent_chain(final_authority)
        _need(source_authority['parent_cumulative_authority'] == final_authority['parent_cumulative_authority']
              and any(node['scope'] == prerequisites['source_scope']
                      and node['authority'] == prerequisites['source_authority'] for node in parent_chain),
              'source_phase_not_in_parent_chain')
        _need(source_scope['candidate'] == scope['candidate']
             and source_authority['scope_sha256'] == prerequisites['source_scope']['sha256']
             and source_closed['scope_sha256'] == prerequisites['source_scope']['sha256'], 'source_revision_binding_invalid')
        # Recompute per-record quality; a terminal closed phase is not a pass.
        actual = e._candidate_phase_status(Path(prerequisites['source_scope']['file']), verified=verified)
        _need(source_closed['report'] == prerequisites['source_phase'], 'source_closed_report_mismatch')
        saved = _read(prerequisites['source_phase'])
        phase = saved['phase']
        _need(phase['candidate'] == actual['candidate'] and phase['cases'] == actual['cases'], 'source_report_projection_changed')
        _need(actual['candidate'] == scope['candidate']
             and len(actual['cases']) == len(source_scope['cases']) > 0
             and all(c['state'] == 'quality_passed' and c['cli_starts'] == 1
                     and c['quality']['status'] == 'pass' for c in actual['cases']), 'source_cohort_not_qualified')
        for case in actual['cases']:
            quality = flow_evals.read_financial_quality_report(case['quality']['quality_report'])
            _need(len(quality['cases']) == 1 and quality['cases'][0]['source_kind'] == 'real_archive',
                  'source_cohort_not_real')
        return actual

    def full_source_qualification(self, request):
        quality = flow_evals.read_financial_quality_report(request['prerequisites']['financial_qualification'])
        _need(quality['eligible'] is True and not quality['missing_entertainment_scenarios'],
             'full_source_financial_qualification_missing')
        source_scope = _read(request['prerequisites']['source_scope'])
        original_cases = {}
        for row, path, _plan in _pair_rows(source_scope):
            audit = (_read(row['audit_binding']) if row.get('audit_binding') else
                     batch._object(path.parent, 'candidate-audit-binding.private.json'))
            original = flow_evals.read_financial_quality_report(audit['quality_report'])
            _need(len(original['cases']) == 1, 'source_quality_case_not_unique')
            case = original['cases'][0]
            _need(case['item_key'] not in original_cases, 'source_quality_case_not_unique')
            original_cases[case['item_key']] = (original['schema_version'], case)
        _need(len(quality['cases']) == len(original_cases), 'source_quality_window_mismatch')
        window_fields = {'history_scope_resolution', 'window_expectations_qualified'}
        for case in quality['cases']:
            _need(case['item_key'] in original_cases and case['source_kind'] == 'real_archive',
                  'source_quality_window_mismatch')
            timing_qualified = (case.get('window_expectations_qualified') if quality['schema_version'] == 3
                                else case['expectations_frozen_before_replay'])
            _need(timing_qualified is True, 'source_quality_expectations_unqualified')
            schema, original = original_cases[case['item_key']]
            if quality['schema_version'] == 3 and schema != 3:
                # A mixed schema3 cohort adds these two projections to normal
                # older cases. It cannot upgrade a historical conflict or time.
                _need({key: value for key, value in case.items() if key not in window_fields} == original
                      and case['window_expectations_qualified'] == original['expectations_frozen_before_replay']
                      and case['history_scope_resolution'] == {
                          'status': 'not_required' if original['historical_crosscheck'] == 'complete' else 'unresolved'},
                      'source_quality_window_mismatch')
            else:
                _need(case == original, 'source_quality_window_mismatch')

    def qualified_source(self, request, scope, *, verified=None):
        self.source_quality(request, scope, verified=verified)
        self.full_source_qualification(request)
        # QA records bind original immutable source. New caller/report optimizations
        # cannot silently make old source-host bytes interchangeable with new bytes.
        for row, _path, plan in _pair_rows(scope):
            candidate = Path(plan['arms']['candidate'])
            manifest, _files, snapshot = self.adapter.archive_sources(_ref(candidate / 'manifest.json'))
            _need(manifest['source_revision'] == scope['candidate']
                 and snapshot == plan['preparations']['candidate']['source_snapshot_sha256'], 'source_host_snapshot_mismatch')
            if (candidate / 'result.json').exists():
                # Consumption legitimately changes state/observability inputs.
                # Preserve the original manifest and verify actual correspondence.
                _terminal(plan, 'candidate', batch.build_report(candidate))
            else:
                _unowned(plan, 'candidate')
                e._verify_sources(candidate, Path(plan['repository']))

    def selected_pair(self, row):
        return self.adapter.prepare_pair(row['plan'], row.get('reuse', {}), row.get('snapshot_audit'))

    def authority_for(self, scope_ref, authority_ref):
        self._verify_runtime()
        authority = _read(authority_ref)
        parent = _read(authority['parent_cumulative_authority'])
        _need(authority['scope_sha256'] == scope_ref['sha256'] and authority['scope_file'] == scope_ref['file']
             and authority['runner_sha256'] == batch._sha(self.caller_file)
             and authority['phase'] == 'final_production_code_comparison'
             and authority['oa_write_authorized'] is False and parent['oa_write_authorized'] is False
             and authority['cumulative_maximum_cli_starts'] == parent['maximum_cli_starts'] == 120
             and authority['candidate_diagnosis_limit'] == parent['phase_limits']['candidate_diagnosis'] == 70
             and type(authority['maximum_cli_starts']) is int and 0 < authority['maximum_cli_starts'] <= 40
             and parent['same_source_model_retries'] is False
             and parent['quality_failure_stops_revision_expansion'] is True
             and bool(authority['authority_reference'])
             and all(isinstance(authority.get(k), str) for k in ('not_before', 'expires_at')), 'authorization_not_bound')
        e._valid_authority_time(authority)
        return authority

    def native_ready(self, row, scope, selection):
        plan = _read(row['plan'])
        for arm in selection['pending_arms']:
            if arm == 'baseline':
                record = scope.get('baseline_native_contract')
                _need(record is not None, 'source_native_contract_unavailable_baseline')
            else:
                record = scope['native_contract']
                archived = Path(plan['arms'][arm]) / 'calling-source/apps/oa_finance_preaudit/native_review_contract.py'
                _need(archived.is_file(), 'source_native_contract_unavailable_' + arm)
            _read(record)
            result = e._host(Path(row['plan']['file']), arm,
                             native_record=Path(record['file']))
            if arm == 'baseline':
                _need(plan['source_roles']['baseline'] == 'production_code_replay'
                      and result.get('status') == 'verified_baseline_observation'
                      and result.get('baseline_comparison_ready') is True
                      and result.get('paid_dispatch_ready') is False
                      and result.get('input_complete') is False
                      and result.get('sample_eligible') is False,
                      'source_native_contract_unverified_baseline')
                binding = result['binding']
                _need(binding['source_revision'] == plan['source_revisions']['baseline']
                      and binding['source_snapshot_sha256'] == plan['preparations']['baseline']['source_snapshot_sha256'],
                      'baseline_native_source_binding_mismatch')
            else:
                _need(result.get('status') == 'verified' and result.get('paid_dispatch_ready') is True,
                      'source_native_contract_unverified_candidate')

    def _budget(self, authority, scope, request, remaining, *, verified=None):
        budget = _parent_budget(authority, scope, request['scope'], verified=verified)
        _need(budget['global_cli_starts'] + remaining <= authority['cumulative_maximum_cli_starts']
              and budget['phase_cli_starts'] + remaining <= authority['maximum_cli_starts']
              and budget['diagnostic_cli_starts'] <= authority['candidate_diagnosis_limit'],
              'start_budget_exhausted')
        return budget

    def _previous_quality(self, scope, target, *, verified=None):
        """Fresh pairs advance only after the preceding actual outputs are audited."""
        verified = verified or batch._VerifiedReports()
        for row, path, plan in _pair_rows(scope):
            if row['alias'] == target['alias']:
                return
            reused = row.get('reuse', {})
            for arm in ARMS:
                if arm in reused:
                    self.adapter.match_target(row['plan'], arm, reused[arm], row.get('snapshot_audit'))
                    continue
                _need(_side_count(path, plan, arm) == 1, 'previous_case_not_observed')
                report = verified.read(Path(plan['arms'][arm]))
                _terminal(plan, arm, report)
                if arm == 'candidate':
                    quality = e._candidate_phase_quality(row, path, report, verified=verified)
                else:
                    quality = self._baseline_quality(row, path, report)
                if arm == 'candidate':
                    _need(quality['status'] == 'pass', 'previous_quality_' + quality['status'])
                else:
                    # Trustworthy baseline defects are comparison evidence.
                    # An incomplete original audit cannot admit the next case.
                    _need(quality.get('audit_complete') is True and quality['status'] in {'pass', 'fail'},
                          'previous_baseline_quality_' + quality['status'])
        raise ValueError('scope_case_not_found')

    @staticmethod
    def _baseline_quality(row, path, report):
        """Baseline has its own original rule/checklist and consumed output binding."""
        from . import flow_evals
        binding_file = path.parent / 'baseline-audit-binding.private.json'
        if row.get('baseline_audit_binding') is None and not binding_file.exists():
            return {'status': 'pending'}
        refs = (_read(row['baseline_audit_binding']) if row.get('baseline_audit_binding') is not None
                else batch._object(path.parent, binding_file))
        _need(refs['checklist'] == row['baseline_checklist']
              and refs['expectations'] == row['baseline_expectations'], 'baseline_expectations_binding_mismatch')
        saved = flow_evals.read_financial_quality_report(refs['quality_report'])
        rebuilt = flow_evals.build_financial_quality_report([{
            'item_key': report['financial_binding']['item_key'],
            'archive': _ref(Path(_read(row['plan'])['arms']['baseline']) / 'manifest.json'),
            'checklist': refs['checklist'], 'assessment': refs['assessment'],
            'expectations': refs['expectations']}])
        _need(saved == rebuilt, 'baseline_quality_projection_changed')
        case = saved['cases'][0]
        complete = (case['binding_status'] == 'verified_consumed_correspondence'
                    and case['original_final_contract'] == 'verified_snapshot_schema'
                    and case['result_audit_complete'] is True
                    and case['complete_review_scope'] is True)
        return {'status': case['verdict'] if complete else 'unverified', 'audit_complete': complete,
                'quality_report': refs['quality_report']}

    def _same_source_attempt(self, authority, row, pending):
        target = _read(row['plan'])
        for reference in authority['prior_plans']:
            prior = _read({key: reference[key] for key in ('file', 'sha256')})
            for arm in pending:
                if (prior['source_revisions'][arm] == target['source_revisions'][arm]
                        and prior['preparations'][arm]['conditions']['sample'] == target['preparations'][arm]['conditions']['sample']):
                    _need(_side_count(Path(reference['file']), prior, arm) == 0,
                          'same_source_case_already_attempted')

    def validate_or_run(self, request, *, run=False, stop_requested=lambda: False):
        """Only this entry owns qualification, parent locking, claims and dispatch."""
        started = time.monotonic()
        authority = self.authority_for(request['scope'], request['authority'])
        progress = {'phase_claim_written': False, 'new_cli_starts': 0}
        with e._candidate_parent_lock(authority['parent_cumulative_authority']):
            scope = self.verify_scope(request['scope'])
            row = next((row for row in scope['cases'] if row['alias'] == request['alias']), None)
            _need(row is not None, 'scope_case_not_found')
            stop_file = Path(request['scope']['file']).parent / 'batch-stop.private.json'
            _need(not stop_file.exists(), 'phase_already_stopped')
            selection = self.selected_pair(row)
            pending = selection['pending_arms']
            _need(pending, 'pair_already_observed_no_retry')
            verified = batch._VerifiedReports()
            self.qualified_source(request, scope, verified=verified)
            try:
                self._previous_quality(scope, row, verified=verified)
            except ValueError as error:
                if run and str(error) in {'previous_quality_fail', 'previous_quality_unverified',
                                          'comparison_output_rejected', 'comparison_result_untrusted',
                                          'comparison_process_or_usage_unverified'}:
                    e._candidate_phase_stop(Path(request['scope']['file']), row['alias'], str(error))
                raise
            self._same_source_attempt(authority, row, pending)
            budget = self._budget(authority, scope, request, len(pending), verified=verified)
            self.native_ready(row, scope, selection)
            for arm in pending:
                plan = _read(row['plan'])
                current = e._host(Path(row['plan']['file']), arm)
                _need(all(current[key] == plan['preparations'][arm][key] for key in
                          ('conditions', 'delivery', 'input_sha256', 'source_snapshot_sha256')),
                      'prepared_input_drift')
            if not run:
                return dict(status='validated_references_only', selection=selection,
                            budget=budget, actual_native_dispatch_enabled=True,
                            automated_preflight_seconds=round(time.monotonic() - started, 6), **progress)
            _need(not stop_requested(), 'cancelled_before_start')
            # Presence is checked only after admission. Values are never read here.
            _need('OA_REVIEW_API_KEY' in os.environ, 'reviewer_credentials_unavailable')
            scope_file, authority_file = Path(request['scope']['file']), Path(request['authority']['file'])
            self.authority_for(request['scope'], request['authority'])
            self.verify_scope(request['scope'])
            claim_file = scope_file.parent / 'candidate-phase-claim.private.json'
            existed = claim_file.exists()
            try:
                e._claim_candidate_phase(scope_file, authority_file, authority)
            finally:
                progress['phase_claim_written'] = not existed and claim_file.exists()
            plan_file = Path(row['plan']['file'])
            operation_starts = _counted_sides(scope)
            operation_reports = verified
            preflight_seconds = time.monotonic() - started

            def revalidate():
                # New operation reader after every execute. Historical archives
                # cannot remain trusted merely because preflight once succeeded.
                nonlocal operation_starts, operation_reports, preflight_seconds
                recheck_started = time.monotonic()
                current_authority = self.authority_for(request['scope'], request['authority'])
                current_scope = self.verify_scope(request['scope'])
                observed = _counted_sides(current_scope)
                if observed != operation_starts:
                    operation_reports = batch._VerifiedReports()
                    operation_starts = observed
                self.source_quality(request, current_scope, verified=operation_reports)
                self.full_source_qualification(request)
                current_plan = _read(row['plan'])
                remaining = sum(_side_count(plan_file, current_plan, arm) == 0 for arm in pending)
                self._budget(current_authority, current_scope, request, remaining, verified=operation_reports)
                _need(not stop_file.exists(), 'phase_already_stopped')
                preflight_seconds += time.monotonic() - recheck_started

            result = _execute_pending_pair(plan_file, selection, authority,
                                           revalidate=revalidate, stop_requested=stop_requested)
            if result['status'] == 'stopped':
                try:
                    e._candidate_phase_stop(scope_file, row['alias'], result['reason'])
                except (OSError, ValueError):
                    result['stop_record'] = 'persistence_failed'
            result['phase_claim_written'] = progress['phase_claim_written']
            result['automated_preflight_seconds'] = round(preflight_seconds, 6)
            try:
                result['budget'] = _parent_budget(authority, scope, request['scope'])
            except (OSError, ValueError, KeyError, TypeError):
                result['budget'] = {'status': 'unverified', 'global_cli_starts': 'unknown'}
            return result

    def inspect_readiness(self, request):
        """Inspect all existing gates without claims, environment values or dispatch."""
        verified = batch._VerifiedReports()
        selected = None

        def scope():
            return self.verify_scope(request['scope'])

        def authority():
            return self.authority_for(request['scope'], request['authority'])

        def row():
            value = next((item for item in scope()['cases'] if item['alias'] == request['alias']), None)
            _need(value is not None, 'scope_case_not_found')
            return value

        def selection():
            nonlocal selected
            if selected is None:
                selected = self.selected_pair(row())
            _need(selected['pending_arms'], 'pair_already_observed_no_retry')
            return copy.deepcopy(selected)

        def source_quality():
            target = _read(request['scope']) if request.get('scope') else _read(request['prerequisites']['source_scope'])
            self.source_quality(request, target, verified=verified)

        checks = {
            'scope': scope, 'authority': authority, 'source_quality': source_quality,
            'financial_qualification': lambda: self.full_source_qualification(request),
            'selection': selection,
            'budget': lambda: self._budget(authority(), scope(), request, len(selection()['pending_arms']), verified=verified),
            'native_contract': lambda: self.native_ready(row(), scope(), selection()),
            'dispatch_wiring': lambda: None}
        def inspect(check):
            def run():
                check()
            return run

        return {**e._comparison_readiness({name: inspect(check) for name, check in checks.items()}), 'actual_native_dispatch_enabled': True,
                'new_model_starts': 0, 'new_oa_writes': 0,
                'financial_qualification_granted': False, 'financial_twenty_complete': False,
                'production_acceptance': False}
