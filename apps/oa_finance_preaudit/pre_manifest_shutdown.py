"""Seal a failed invocation whose unchanged authority proves Controller never began."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import time

from . import app_review_shutdown as shutdown, controller_handoff, host_chat_review as host, maintenance_runtime, native_evidence

MODULE = 'apps.oa_finance_preaudit.pre_manifest_shutdown'
ROOT = Path(__file__).resolve().parents[2]
FILE = 'pre-manifest-shutdown.json'
BLOCKED = 'host_review_pre_manifest_untrusted'
SOURCE_FILES = ('scripts/finance_preaudit_run.py', 'scripts/finance_preaudit_concurrency_probe.py',
                'apps/oa_finance_preaudit/controller_handoff.py')


def _candidate() -> dict:
    revision, dirty = controller_handoff._code_state()
    if dirty is not False or controller_handoff._REVISION.fullmatch(revision) is None:
        raise ValueError(BLOCKED)
    if subprocess.run(['git', 'rev-parse', 'origin/main'], cwd=ROOT,
                      capture_output=True, text=True, check=True).stdout.strip() != revision:
        raise ValueError(BLOCKED)
    return {'root': str(ROOT), 'revision': revision, 'interpreter': str(ROOT / '.venv/bin/python'),
            'module_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def _original(target: Path, binding: dict) -> tuple[dict, bytes, dict]:
    from scripts import finance_preaudit_run as runner
    path = Path(binding['receipt_file'])
    if path != target / 'runtime/control/executions' / binding['invocation_id'] / 'execution.json':
        raise ValueError(BLOCKED)
    if target == runner.PROJECT_ROOT and controller_handoff.manifest_file() == target / 'runtime/control/rolling-controller-manifest.json':
        report = runner.read_execution(**binding)
    else:
        report = maintenance_runtime.read_original_execution(target, maintenance_runtime.OriginalExecution(
            path, binding['owner_task_id'], binding['tool_session_id']))
    raw, execution = host._read(path)
    if (report['state'] != 'exited' or execution.get('result') != 'blocked'
            or execution.get('exit_code') != 1 or execution.get('exit_signal') is not None
            or execution.get('review_configuration') != host.IDENTITY
            or execution.get('run_id') is not None or execution.get('controller_instance_id') is not None
            or execution.get('controller_state') != 'unknown' or execution.get('children_reaped') != 'unknown'
            or execution.get('received_signals') != [] or execution.get('stop_requested_by_supervisor') is not False
            or any(key in execution for key in ('primary_failure', 'cleanup_failures', 'stop_request_error', 'stop_wait_error'))
            or any(path.with_name(name).exists() or path.with_name(name).is_symlink()
                   for name in ('controller-manifest.json', 'legacy-controller-manifest.json'))):
        raise ValueError(BLOCKED)
    for name in ('controller', 'supervisor'):
        identity = execution.get(name + '_process')
        if (not host.guard._valid_identity(identity) or name == 'controller' and host.guard.group_alive(identity['pgid'])
                or host.guard.process_identity(identity['pid']) == identity):
            raise ValueError(BLOCKED)
    logs = {name: runner._log_evidence(path.with_name(name)) for name in ('stdout.log', 'stderr.log')}
    payload = runner._object(logs['stdout.log'][1])
    expected = {'schema_version': 1, 'kind': 'preaudit_production_concurrency_probe_failed',
        'error_code': 'production_rolling_reconciliation_invalid', 'notification': {
            'delivery_state': 'not_authorized', 'error_code': 'controller_failed',
            'kind': 'oa_session_alert_receipt', 'schema_version': 1}}
    if payload != expected or logs['stderr.log'][1] != b'':
        raise ValueError(BLOCKED)
    return report, raw, execution


def _stage(target: Path, execution: dict) -> dict:
    from scripts import finance_preaudit_run as runner
    snapshot = controller_handoff.manifest_snapshot(target / 'runtime/control/rolling-controller-manifest.json')
    if snapshot[0] is None:
        raise ValueError(BLOCKED)
    manifest = json.loads(controller_handoff.manifest_payload(snapshot))
    started = runner._timestamp(execution['started_at']).timestamp()
    if (manifest['invocation_id'] == execution['invocation_id'] or manifest['controller_pid'] == execution['controller_pid']
            or manifest['state'] not in {'completed', 'failed'} or manifest['children_reaped'] is not True
            or runner._timestamp(manifest['completed_at']).timestamp() > started
            or any(identity[2] / 1e9 >= started or identity[3] / 1e9 >= started for _path, identity in snapshot[2])):
        raise ValueError(BLOCKED)
    stage = {'pointer_sha256': hashlib.sha256(snapshot[0]).hexdigest(),
            'manifest_sha256': hashlib.sha256(snapshot[1]).hexdigest(),
            'identities': [[path, list(identity)] for path, identity in snapshot[2]],
            'prior_invocation_id': manifest['invocation_id'], 'prior_run_id': manifest['run_id']}
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=target,
        capture_output=True, text=True, check=True).stdout.strip()
    if controller_handoff._REVISION.fullmatch(revision) is None:
        raise ValueError(BLOCKED)
    sources = {}
    for name in SOURCE_FILES:
        path = target / name
        identity = controller_handoff._manifest_identity(path)
        content = shutdown.runtime_control.read_control_bytes(path)
        committed = subprocess.run(['git', 'show', revision + ':' + name], cwd=target,
            capture_output=True, check=True).stdout
        if (identity[2] / 1e9 >= started or identity[3] / 1e9 >= started or content != committed
                or controller_handoff._manifest_identity(path) != identity):
            raise ValueError(BLOCKED)
        sources[name] = {'identity': list(identity), 'sha256': hashlib.sha256(content).hexdigest()}
    stage.update(source_revision=revision, source_files=sources)
    before = execution.get('pre_launch_controller')
    if before is not None and (before.get('status') != 'recorded'
            or before.get('pointer_sha256') != stage['pointer_sha256']
            or before.get('manifest_sha256') != stage['manifest_sha256']
            or before.get('identities') != stage['identities']
            or not started <= before.get('observed_at', 0) <= runner._timestamp(execution['completed_at']).timestamp()):
        raise ValueError(BLOCKED)
    return stage


def _proofs(*, target: Path, binding: dict, reviewers: list[dict], source: Path,
            interrupt_lines: list[int], status_line: int, wait_line: int, launch_line: int) -> dict:
    from scripts import finance_preaudit_run as runner
    report, raw, execution = _original(target, binding)
    started = runner._timestamp(execution['started_at']).timestamp()
    completed = runner._timestamp(execution['completed_at']).timestamp()
    native, launch = native_evidence.read(source, binding['owner_task_id'], launch_line, output=True)
    payload = native['anchor']['payload']
    calls = native_evidence.execution_calls(payload.get('input', ''))
    if payload.get('name') not in {'exec', 'functions.exec'} or len(calls) != 1 or calls[0][0] != 'exec_command':
        raise ValueError(BLOCKED)
    options = calls[0][1]
    argv = shlex.split(options['cmd'])
    results = native_evidence.execution_results(launch.pop('output'))
    if (argv[:3] != ['.venv/bin/python', '-m', 'scripts.finance_preaudit_run']
            or argv[3:10] != ['--owner-task-id', binding['owner_task_id'], '--review-provider', 'openai',
                             '--review-transport', 'host_chat', '--']
            or '--rolling' not in argv[10:] or shlex.join(argv) != options['cmd']
            or options.get('workdir', native['header'].get('cwd')) != str(target)
            or len(results) != 1 or str(results[0].get('session_id')) != binding['tool_session_id']
            or results[0].get('exit_code') is not None
            or runner._timestamp(native['anchor']['timestamp']).timestamp() > started):
        raise ValueError(BLOCKED)
    sources = shutdown._unregistered_sources(owner=binding['owner_task_id'], reviewers=reviewers,
        source=source, execution=execution, report=report, started=started, completed=completed,
        interrupt_lines=interrupt_lines, status_line=status_line, wait_line=wait_line,
        receipt_file=Path(binding['receipt_file']), tool_session_id=binding['tool_session_id'], strict_wait=True)
    if any(item['spawn_source']['line'] <= launch['output_line'] for item in sources['reviewers']):
        raise ValueError(BLOCKED)
    return {'original_execution': binding, 'execution_sha256': hashlib.sha256(raw).hexdigest(),
            'output_files': report['output_files'], 'launch_source': launch, **sources}


def observe(*, target: Path, binding: dict, reviewers: list[dict], source: Path,
            interrupt_lines: list[int], status_line: int, wait_line: int, launch_line: int) -> dict:
    with maintenance_runtime.target_runtime_leases(target):
        proof = _proofs(target=target, binding=binding, reviewers=reviewers, source=source,
            interrupt_lines=interrupt_lines, status_line=status_line, wait_line=wait_line, launch_line=launch_line)
        execution = host._read(Path(binding['receipt_file']))[1]
        stage = _stage(target, execution)
        candidate = _candidate()
        if _stage(target, execution) != stage:
            raise ValueError(BLOCKED)
        return {'kind': 'preaudit_app_pre_manifest_observation', 'status': 'ready',
            'target_root': str(target), 'observed_at': time.time(), 'candidate': candidate,
            'controller_started': False, 'original_children_reaped': 'unknown',
            'current_processes_exited': True, 'no_actions_or_writes': True,
            'authority': stage, **proof}


def _inputs(observed: dict) -> dict:
    return {'target': Path(observed['target_root']), 'binding': observed['original_execution'],
        'reviewers': [{'session_file': item['child_source']['session_file'], 'spawn_line': item['spawn_source']['line']}
                      for item in observed['reviewers']],
        'source': Path(observed['children_source']['session_file']),
        'interrupt_lines': [item['line'] for item in observed['interrupt_sources']],
        'status_line': observed['children_source']['line'], 'wait_line': observed['driver_wait_source']['line'],
        'launch_line': observed['launch_source']['line']}


def command(*, target: Path, binding: dict, reviewers: list[dict], source: Path,
            interrupt_lines: list[int], status_line: int, wait_line: int, launch_line: int,
            candidate: dict | None = None) -> dict:
    candidate = candidate or _candidate()
    args = [candidate['interpreter'], '-B', '-m', MODULE, '--operation', 'observe',
        '--target-root', str(target), '--expected-revision', candidate['revision'],
        '--owner-task-id', binding['owner_task_id'], '--receipt-file', binding['receipt_file'],
        '--invocation-id', binding['invocation_id'], '--tool-session-id', binding['tool_session_id'],
        '--source-session-file', str(source), '--source-line', str(status_line),
        '--wait-line', str(wait_line), '--launch-line', str(launch_line)]
    for reviewer, interrupt in zip(reviewers, interrupt_lines):
        args += ['--reviewer-session-file', reviewer['session_file'], '--spawn-line', str(reviewer['spawn_line']),
                 '--interrupt-line', str(interrupt)]
    return {'cmd': shlex.join(args), 'workdir': candidate['root']}


def _observation_source(source: Path, owner: str, line: int) -> tuple[dict, dict]:
    native, proof = native_evidence.read(source, owner, line, output=True)
    results = native_evidence.execution_results(proof.pop('output'))
    if len(results) != 1:
        raise ValueError(BLOCKED)
    output = results[0]
    if not isinstance(output, dict) or output.get('exit_code') != 0 or not isinstance(output.get('output'), str):
        raise ValueError(BLOCKED)
    observed = json.loads(output['output'])
    candidate = observed['candidate']
    target, root = Path(observed['target_root']), Path(candidate['root'])
    if (not root.is_absolute() or '..' in root.parts
            or root != target and not root.is_relative_to(target.parent / 'oa-workspace-dev')
            or candidate['interpreter'] != str(root / '.venv/bin/python')
            or controller_handoff._REVISION.fullmatch(candidate['revision']) is None):
        raise ValueError(BLOCKED)
    committed = subprocess.run(['git', 'show', candidate['revision'] + ':' +
        'apps/oa_finance_preaudit/pre_manifest_shutdown.py'], cwd=ROOT, capture_output=True, check=True).stdout
    if hashlib.sha256(committed).hexdigest() != candidate['module_sha256']:
        raise ValueError(BLOCKED)
    expected = command(**_inputs(observed), candidate=observed['candidate'])
    anchor = native['anchor']
    calls = native_evidence.execution_calls(str(anchor['payload'].get('input', '')))
    if (anchor['payload'].get('name') not in {'exec', 'functions.exec'}
            or calls != [('exec_command', expected)]
            or observed.get('kind') != 'preaudit_app_pre_manifest_observation' or observed.get('status') != 'ready'
            or observed['original_execution']['owner_task_id'] != owner
            or observed['children_source']['session_file'] != str(source.absolute())):
        raise ValueError(BLOCKED)
    from scripts import finance_preaudit_run as runner
    returned, _ = native_evidence.read(source, owner, proof['output_line'])
    if not (runner._timestamp(anchor['timestamp']).timestamp() <= observed['observed_at']
            <= runner._timestamp(returned['anchor']['timestamp']).timestamp() <= time.time()):
        raise ValueError(BLOCKED)
    return observed, proof


def _revalidate(observed: dict) -> dict:
    proof = _proofs(**_inputs(observed))
    if any(observed.get(key) != value for key, value in proof.items()):
        raise ValueError(BLOCKED)
    return proof


def seal(*, target: Path, owner: str, source: Path, observation_line: int) -> dict:
    observed, anchor = _observation_source(source, owner, observation_line)
    if observed['target_root'] != str(target):
        raise ValueError(BLOCKED)
    with maintenance_runtime.target_runtime_leases(target):
        _revalidate(observed)
        execution = host._read(Path(observed['original_execution']['receipt_file']))[1]
        if _stage(target, execution) != observed['authority']:
            raise ValueError(BLOCKED)
        result = {'kind': 'preaudit_app_pre_manifest_shutdown', 'status': 'verified',
                  'observation': observed, 'observation_source': anchor, 'write_state': 'not_requested'}
        path = Path(observed['original_execution']['receipt_file']).with_name(FILE)
        if path.exists():
            if sealed(path.with_name('execution.json')) != result:
                raise ValueError(BLOCKED)
        else:
            host._write_new(path, result)
        return result


def sealed(receipt_file: Path) -> dict:
    saved = host._read(receipt_file.with_name(FILE))[1]
    proof = saved['observation_source']
    observed, anchor = _observation_source(Path(proof['session_file']),
        saved['observation']['original_execution']['owner_task_id'], proof['line'])
    if (observed != saved['observation'] or anchor != proof
            or observed['original_execution']['receipt_file'] != str(receipt_file.absolute())
            or saved != {'kind': 'preaudit_app_pre_manifest_shutdown', 'status': 'verified',
                         'observation': observed, 'observation_source': anchor, 'write_state': 'not_requested'}):
        raise ValueError(BLOCKED)
    _revalidate(observed)
    return saved


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation', choices=('observe', 'seal'), required=True)
    parser.add_argument('--target-root', required=True, type=Path)
    parser.add_argument('--expected-revision', required=True)
    parser.add_argument('--owner-task-id', required=True)
    parser.add_argument('--receipt-file', type=Path)
    parser.add_argument('--invocation-id')
    parser.add_argument('--tool-session-id')
    parser.add_argument('--source-session-file', required=True, type=Path)
    parser.add_argument('--source-line', required=True, type=int)
    parser.add_argument('--wait-line', type=int)
    parser.add_argument('--launch-line', type=int)
    parser.add_argument('--reviewer-session-file', type=Path, action='append')
    parser.add_argument('--spawn-line', type=int, action='append')
    parser.add_argument('--interrupt-line', type=int, action='append')
    args = parser.parse_args(argv)
    candidate = _candidate()
    if (candidate['revision'] != args.expected_revision
            or subprocess.run(['git', 'branch', '--show-current'], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip() != 'main'):
        raise ValueError(BLOCKED)
    from . import production_release
    production_release._source(args.target_root, args.expected_revision, production_release.ORIGIN)
    if args.operation == 'seal':
        result = seal(target=args.target_root, owner=args.owner_task_id,
                      source=args.source_session_file, observation_line=args.source_line)
    else:
        if (any(value is None for value in (args.receipt_file, args.invocation_id, args.tool_session_id, args.wait_line, args.launch_line))
                or any(len(values or []) != 3 for values in
                       (args.reviewer_session_file, args.spawn_line, args.interrupt_line))):
            parser.error('observe requires the original execution and three native shutdown sources')
        result = observe(target=args.target_root, binding={'receipt_file': str(args.receipt_file.absolute()),
            'owner_task_id': args.owner_task_id, 'invocation_id': args.invocation_id,
            'tool_session_id': args.tool_session_id}, reviewers=[{'session_file': str(path), 'spawn_line': line}
            for path, line in zip(args.reviewer_session_file, args.spawn_line)], source=args.source_session_file,
            interrupt_lines=args.interrupt_line, status_line=args.source_line, wait_line=args.wait_line,
            launch_line=args.launch_line)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
