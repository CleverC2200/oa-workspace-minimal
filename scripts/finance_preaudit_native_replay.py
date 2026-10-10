#!/usr/bin/env python3
"""Explicit native regression using recorded evidence and preview-only closure."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)

def read(path):
    return json.loads(Path(path).read_text())

def path_rewrite(value, old, new):
    if isinstance(value, dict):
        return {k: path_rewrite(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [path_rewrite(v, old, new) for v in value]
    if isinstance(value, str) and (value == str(old) or value.startswith(str(old) + '/')):
        return str(new) + value[len(str(old)):]
    return value

def engine_for(root, material=None, original=None):
    from apps.oa_finance_preaudit.flow_engine import FlowEngine
    if original is None:
        return FlowEngine(root=root, discover_candidates=lambda _key: [],
                          collect_material=lambda _id, _root: (_ for _ in ()).throw(RuntimeError('recorded_collector_only')))
    def collect(request_id, attempt):
        if request_id != original['request_id']:
            raise RuntimeError('recorded_request_mismatch')
        mapping = []
        for directory in ('attachments', 'prepared'):
            source = original['archived_attempt'] / directory
            if source.exists():
                for p in source.rglob('*'):
                    if p.is_symlink():
                        raise RuntimeError('source_symlink_forbidden')
                    if p.is_file():
                        relative = p.relative_to(original['archived_attempt'])
                        target = attempt / relative
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        shutil.copyfile(p, target)
                        target.chmod(0o600)
                        if digest(p) != digest(target):
                            raise RuntimeError('copied_evidence_changed')
                        mapping.append({'source': str(p), 'target': str(target), 'sha256': digest(p)})
        rebound = path_rewrite(copy.deepcopy(material), original['original_attempt'], attempt)
        if path_rewrite(rebound, attempt, original['original_attempt']) != material:
            raise RuntimeError('material_rebinding_loss')
        original['mapping'] = mapping
        return rebound
    return FlowEngine(root=root,
        discover_candidates=lambda key: [{'request_id': original['request_id'],
            'received_at': original['received_at'], 'workflow_key': key}], collect_material=collect)

def prepare(args):
    case = next(c for c in read(args.inventory)['cases'] if c['case'] == args.case)
    source = Path(case['material_file']).resolve()
    state_file = Path(case['state_file']).resolve()
    archive = source.parent
    state = read(state_file)
    if state['material_fingerprint'] != 'sha256:' + digest(source):
        raise RuntimeError('original_material_fingerprint_mismatch')
    material = read(source)
    original = {'request_id': state['request_id'], 'received_at': state['received_at'],
        'archived_attempt': archive, 'original_attempt': Path(state['material_file']).parent}
    original_paths = {Path(item['local_readonly_path']).expanduser().resolve()
                      for item in material['resources']}
    original_paths.update(Path(item['path']).expanduser().resolve()
                          for item in material['prepared']['images'])
    evidence_digest = hashlib.sha256()
    for original_path in sorted(original_paths, key=str):
        archived_path = archive / original_path.relative_to(original['original_attempt'].resolve())
        if archived_path.is_symlink() or not archived_path.is_file():
            raise ValueError('original_evidence_unavailable')
        evidence_digest.update(str(original_path).encode('utf-8'))
        evidence_digest.update(b'\0')
        evidence_digest.update(archived_path.read_bytes())
        evidence_digest.update(b'\0')
    if state['evidence_fingerprint'] != 'sha256:' + evidence_digest.hexdigest():
        raise ValueError('original_evidence_fingerprint_mismatch')
    project = args.audit / 'native-projects' / args.label / args.case / 'project'
    audit = args.audit / 'runs' / args.label / args.case
    if project.exists() or audit.exists():
        raise RuntimeError('replay_label_case_already_exists')
    project.mkdir(parents=True, mode=0o700)
    subprocess.run(['git', 'init', '--quiet', str(project)], check=True)
    audit.mkdir(parents=True, mode=0o700)
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=args.project).decode().split('\0')
    source_manifest = []
    for name in tracked:
        if not name or not (name.startswith('apps/') or name.startswith('.agents/skills/')
                or name.startswith('docs/agents/') or name in {'AGENTS.md', 'CONTRIBUTING.md', 'CONTEXT.md', 'GLOSSARY.md'}):
            continue
        if Path(name).name in {'regression-cases.json', 'confirmed-feedback.md'}:
            continue
        p = args.project / name
        if p.is_symlink() or not p.is_file():
            raise RuntimeError('source_snapshot_invalid')
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(p, target)
        target.chmod(0o600)
        source_manifest.append({'path': name, 'sha256': digest(p)})
    root = project / 'runtime' / 'finance_preaudit_flows'
    engine = engine_for(root, material, original)
    action = engine.next_action('general_expense', mode='preview')
    if action.get('action') != 'review_required' or action.get('write_authorized') is not False or action.get('mode') != 'preview':
        dump(audit / 'prepare-failure.json', action)
        raise RuntimeError('preview_preparation_failed:' + str(action.get('reason')))
    fresh_state = read(action['state_file'])
    prepared_material = read(fresh_state['material_file'])
    expected_rebound = path_rewrite(material, original['original_attempt'], Path(action['action_file']).parent)
    changed_top_level = [k for k in sorted(set(prepared_material) | set(expected_rebound))
                         if prepared_material.get(k) != expected_rebound.get(k)]
    manifest = {'schema_version': 1, 'kind': 'historical_payment_preview_replay',
        'case': args.case, 'label': args.label, 'project_root': str(project), 'engine_root': str(root),
        'action_file': action['action_file'], 'mode': 'preview', 'write_authorized': False,
        'original_material': str(source), 'original_material_sha256': digest(source),
        'original_state_sha256': digest(state_file),
        'original_evidence_fingerprint_verified': state['evidence_fingerprint'], 'original_path_prefix': str(original['original_attempt']),
        'archived_path_prefix': str(archive), 'fresh_material_sha256': digest(fresh_state['material_file']),
        'fresh_evidence_fingerprint': fresh_state['evidence_fingerprint'],
        'material_rebinding_lossless': True, 'current_engine_changed_top_level_fields': changed_top_level,
        'file_mapping': original['mapping'], 'source_manifest': source_manifest,
        'source_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=args.project).decode().strip(),
        'model_has_no_historical_decisions_or_expectations': True, 'oa_client_config_or_submit_callback': False,
        'excluded_reviewer_files': ['confirmed-feedback.md', 'regression-cases.json', 'tests/', 'inventory', 'historical decisions'],
        'worker_source_sha256': digest(args.project / 'apps/oa_finance_preaudit/codex_review_worker.py')}
    dump(audit / 'prepare.json', manifest)
    print(json.dumps({'status': 'prepared', 'case': args.case, 'audit': str(audit),
        'project_root': str(project), 'action_file': action['action_file'], 'image_count': len(prepared_material['prepared']['images']),
        'material_changes': changed_top_level, 'mode': 'preview', 'write_authorized': False}, ensure_ascii=False))

def run(args, *, executor=None):
    from apps.oa_finance_preaudit import codex_review_worker as worker
    from apps.oa_finance_preaudit import process_diagnostics
    audit = args.audit / 'runs' / args.label / args.case
    manifest = read(audit / 'prepare.json')
    if (audit / 'worker-result.json').exists() or (audit / 'native-attempt-1').exists():
        raise RuntimeError('native_run_already_started_do_not_repeat')
    project, action_file = Path(manifest['project_root']), Path(manifest['action_file'])
    if worker.MODEL != 'gpt-6.1-sol' or worker.REASONING_EFFORT != 'medium':
        raise RuntimeError('review_model_identity_changed')
    # Import source must match the snapshot given to the reviewer.
    if digest(Path(worker.__file__)) != manifest['worker_source_sha256']:
        raise RuntimeError('worker_source_changed_since_prepare')
    attempts = []
    def audit_executor(project_root, frozen_action, timeout):
        attempt_audit = audit / f'native-attempt-{len(attempts) + 1}'
        attempt_audit.mkdir(mode=0o700)
        stderr = {}
        started = time.monotonic()
        execution = (executor(project_root, frozen_action, timeout) if executor is not None
                     else worker._execute_codex_raw(project_root, frozen_action, timeout,
                         stderr_summary=stderr, audit_directory=attempt_audit))
        elapsed = time.monotonic() - started
        process_diagnostics.record(frozen_action, execution=execution, elapsed_seconds=elapsed, stderr=stderr)
        record = {'attempt': len(attempts) + 1, 'elapsed_seconds': elapsed,
            'error_code': execution.error_code, 'process_state': execution.process_state,
            'exit_code': execution.exit_code, 'decision_state': execution.decision_state,
            'token_usage': execution.token_usage, 'native_receipt_files': stderr.get('tool_receipt_files', []),
            'native_receipt_unavailable': stderr.get('tool_receipt_unavailable', False)}
        attempts.append(record)
        dump(attempt_audit / 'receipt.json', record)
        return execution
    reviewed = worker.review({'project_root': str(project), 'action_file': str(action_file),
        'timeout_seconds': args.timeout}, executor=audit_executor)
    dump(audit / 'worker-result.json', reviewed)
    result = {'case': args.case, 'review_status': reviewed['status'], 'attempts': attempts,
        'model': reviewed.get('model'), 'reasoning_effort': reviewed.get('reasoning_effort'),
        'requested_service_tier': reviewed.get('requested_service_tier'),
        'execution_transport': 'recorded' if executor is not None else 'native', 'oa_writes': 0,
        'original_material_unchanged': digest(manifest['original_material']) == manifest['original_material_sha256']}
    if reviewed['status'] == 'reviewed':
        decision_file = action_file.with_name('native-preview-decision.json')
        dump(decision_file, reviewed['decision'])
        try:
            closed = engine_for(Path(manifest['engine_root'])).complete_review(action_file, decision_file)
            dump(audit / 'closure.json', closed)
            result.update({'closure_action': closed.get('action'), 'execution_state': closed.get('execution_state'),
                'business_state': closed.get('business_state'), 'write_state': closed.get('write_state'),
                'decision_sha256': digest(decision_file), 'state_sha256': digest(closed['state_file'])})
            result['preview_closed_no_write'] = (closed.get('action') == 'closed'
                and closed.get('execution_state') == 'closed' and closed.get('write_state') == 'not_requested'
                and closed.get('write_authorized') is False)
        except ValueError as error:
            result['closure_error'] = str(error)
    dump(audit / 'result.json', result)
    print(json.dumps(result, ensure_ascii=False))
    if not result.get('preview_closed_no_write'):
        return 1
    return 0

def main(argv=None, *, executor=None):
    parser = argparse.ArgumentParser(description=__doc__)
    phase = parser.add_mutually_exclusive_group(required=True)
    phase.add_argument('--prepare', action='store_true', help='copy immutable historical evidence and freeze a fresh preview; no native call')
    phase.add_argument('--run', action='store_true', help='explicit native Worker.review call then close only preview; one run per label/case')
    parser.add_argument('--case', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args(argv)
    if not all(x and all(c.isalnum() or c in '-_' for c in x) for x in (args.case, args.label)):
        raise ValueError('invalid_case_or_label')
    if not 60 <= args.timeout <= 3600:
        raise ValueError('invalid_timeout')
    args.project = args.project.resolve()
    os.umask(0o077)
    if args.prepare:
        prepare(args)
        return 0
    return run(args, executor=executor)

if __name__ == '__main__':
    sys.exit(main())
