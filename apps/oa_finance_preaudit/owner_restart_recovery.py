"""Audited recovery for sealed owner-restart or explicitly selected user stops."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Callable
from uuid import uuid4

from apps.oa_approval import runtime_control
from . import maintenance_runtime, production_release, controller_handoff


# Run only in the target interpreter, using its installed receipt/process readers.
_ORIGINAL_READER = maintenance_runtime._ORIGINAL_EXECUTION_READER + r'''
import os,time
user_stop = len(sys.argv)>4 and sys.argv[4]=='user_stop'
source_only = len(sys.argv)>5 and sys.argv[5]=='source_only'
prepare_failure = len(sys.argv)>6 and sys.argv[6]=='prepare_failure'
from datetime import datetime
from apps.oa_finance_preaudit import approval_guard as guard, approval_lifecycle as lifecycle, production_release
if any(not Path(module.__file__).resolve().is_relative_to(root) for module in (guard,lifecycle,production_release)):
    raise ValueError('owner_restart_original_untrusted')
path=Path(path)
saved=json.loads(finance_preaudit_run._read_evidence(path))
mirror=json.loads(finance_preaudit_run._read_evidence(path.with_name('controller-manifest.json')))
rolling=root/'runtime/control/rolling-controller-manifest.json'
if (report.get('state')!='exited' or report.get('terminal') is not True
        or type(report.get('exit_code')) is not int or report['exit_code']==0
        or report.get('children_reaped') is not True or report.get('controller_state')!='failed'
        or (not user_stop and saved.get('owner_process_lost') is not True)
        or saved.get('stop_requested_by_supervisor') is not True
        or saved.get('received_signals')!=[] or saved.get('stop_request_error') is not None
        or saved.get('stop_wait_error') is not None
        or mirror!=active_manifest(rolling)):
    raise ValueError('owner_restart_original_untrusted')
stop=production_release._pinned_control(root/'runtime/stop-preaudit.flag')
started=datetime.fromisoformat(saved['started_at']).timestamp()
completed=datetime.fromisoformat(saved['completed_at']).timestamp()
expected_stop = (b'stop_requested:production_probe_prepare_failed\n' if prepare_failure else
                 b'stop_requested\n' if user_stop else
                 b'stop_requested:codex_review_technical_failure\n')
if (stop[1]!=expected_stop
        or not started<=stop[0][2]/1e9<=completed):
    raise ValueError('owner_restart_stop_attribution_untrusted')
for key in ('supervisor_process','controller_process'):
    identity=saved.get(key)
    if (not guard._valid_identity(identity) or guard.process_identity(identity['pid']) is not None
            or guard.group_alive(identity['pgid'])):
        raise ValueError('owner_restart_process_active')
progress=lifecycle._progress(mirror)
if (progress is None or Path(progress['project_root']).resolve()!=root
        or progress['controller']['pid']!=mirror['controller_pid'] or progress['writer_active'] is not False
        or not lifecycle.readonly_reaped(mirror) or not guard.models_reaped()):
    raise ValueError('owner_restart_process_active')
slots=[guard._slot(index) for index in range(guard.MAX_MODEL_WORKERS)]
if any(slot and slot.get('pid') is not None and
        (guard.process_identity(slot['pid']) is not None or guard.group_alive(slot['pid'])) for slot in slots):
    raise ValueError('owner_restart_process_active')
cooldown=guard.cooldown()
if cooldown and time.time()<cooldown['blocked_until'] and not (user_stop and source_only):
    raise ValueError('owner_restart_official_cooldown')
run=lifecycle.run_directory(mirror['run_id'])
def frozen_files():
    files=set()
    for action in progress['actions']:
        for name in ('attachments','prepared'):
            directory=Path(action).parent/name
            if directory.is_symlink():
                raise ValueError('owner_restart_frozen_untrusted')
            if directory.exists():
                for child in directory.rglob('*'):
                    if child.is_symlink():
                        raise ValueError('owner_restart_frozen_untrusted')
                    if child.is_file():
                        files.add(child)
    return files
def paths():
    return sorted({*frozen_files(),path,path.with_name('tool-session.json'),path.with_name('controller-manifest.json'),
        path.with_name('stdout.log'),path.with_name('stderr.log'),rolling,run/'progress.json',
        guard.directory()/'cooldown.json',*run.glob('worker-*.json'),*run.glob('pool-*.json'),
        *(guard._slot_path(index) for index in range(guard.MAX_MODEL_WORKERS)),
        *(Path(action).with_name(name) for action in progress['actions'] for name in
            ('action.json','state.json','material.json','review-packet.json','manual-review-receipt.json',
             'writer-boundary.json','execution-receipt.json','review-decision.json','remark.txt',
             'submission-envelope.json','recovery-receipt.json'))})
def fingerprints():
    return {str(file):production_release._control_proof(production_release._pinned_control(file,private=False,
        max_bytes=64*1024*1024)) if os.path.lexists(file) else None for file in paths()}
files=fingerprints()
required=[path,path.with_name('tool-session.json'),path.with_name('controller-manifest.json'),
    path.with_name('stdout.log'),path.with_name('stderr.log'),rolling,run/'progress.json',
    *(Path(action).with_name(name) for action in progress['actions'] for name in ('action.json','state.json'))]
if (any(files.get(str(file)) is None for file in required) or fingerprints()!=files
        or progress!=lifecycle._progress(mirror)
        or report!=finance_preaudit_run.read_execution(path,owner_task_id=owner,
            invocation_id=path.parent.name,tool_session_id=session)):
    raise ValueError('owner_restart_original_changed')
report['owner_restart_proof']={'kind':'preaudit_owner_restart_proof','read_only':True,
    'original_wait_verified':True,'owner_process_lost':saved.get('owner_process_lost') is True,
    'user_stop':user_stop,'source_only':source_only,'supervisor_stop_verified':True,
    'readonly_reaped':True,'models_reaped':True,'target_root':str(root),'run_id':mirror['run_id'],
    'controller_instance_id':mirror['controller_instance_id'],'stop_attribution':'original_terminal_interval',
    'evidence_files':files}
print(json.dumps(report))
'''


def _read_original(root: Path, original: maintenance_runtime.OriginalExecution, manifest: bytes | tuple,
                   *, user_stop: bool = False, source_only: bool = False,
                   prepare_failure: bool = False) -> dict:
    maintenance_runtime.assert_target_environment(root)
    if (root / '.venv').is_symlink() or not (root / '.venv/bin/python').is_file():
        raise RuntimeError('maintenance_interpreter_untrusted')
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    result = maintenance_runtime._run_readonly(
        [str(root / '.venv/bin/python'), '-B', '-E', '-c', _ORIGINAL_READER,
         str(original.file), original.owner_task_id, original.tool_session_id,
         'user_stop' if user_stop else 'owner_restart', 'source_only' if source_only else 'recovery',
         'prepare_failure' if prepare_failure else 'default'],
        target_root=root, environment=environment, timeout=60, require_reaped=True)
    if result.returncode or len(result.stdout.encode()) > 1_000_000:
        raise RuntimeError('owner_restart_original_untrusted')
    report = json.loads(result.stdout)
    maintenance_runtime.assert_original_execution_safe(root, original, report, manifest)
    return report


def _read_prepare_failure_original(
    root: Path,
    original: maintenance_runtime.OriginalExecution,
    manifest: bytes | tuple,
    stop_controls: tuple,
) -> dict:
    before = production_release._pinned_control(original.file, private=False, max_bytes=64 * 1024 * 1024)
    report = maintenance_runtime.read_original_execution(root, original)
    maintenance_runtime.assert_original_execution_safe(root, original, report, manifest)
    after = production_release._pinned_control(original.file, private=False, max_bytes=64 * 1024 * 1024)
    if before != after:
        raise RuntimeError('prepare_failure_original_changed')
    raw = before[1]
    saved = json.loads(raw)
    if (report.get('state') != 'exited' or report.get('terminal') is not True
            or report.get('exit_code') == 0 or report.get('children_reaped') is not True
            or saved.get('controller_state') != 'failed'):
        raise RuntimeError('prepare_failure_original_untrusted')
    try:
        started = datetime.fromisoformat(saved['started_at']).timestamp()
        completed = datetime.fromisoformat(saved['completed_at']).timestamp()
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError('prepare_failure_original_untrusted') from error
    stop_mtime = stop_controls[0][0][2] / 1e9
    if not started <= stop_mtime <= completed:
        raise RuntimeError('prepare_failure_stop_attribution_untrusted')
    return {
        **report,
        'prepare_failure_recovery_proof': {
            'kind': 'preaudit_prepare_failure_recovery_proof',
            'read_only': True,
            'original_wait_verified': True,
            'stop_attribution': 'original_terminal_interval',
            'children_reaped': True,
            'target_root': str(root),
        },
    }


def recover(
    target_root: Path, *, expected_revision: str,
    automation: production_release.ProductionAutomation,
    original: maintenance_runtime.OriginalExecution,
    confirmed: bool = False, expected_origin: str = production_release.ORIGIN,
    check_live: Callable[[Path], dict] | None = None, user_stop: bool = False,
    prepare_failure: bool = False, selected_action: Path | None = None,
) -> dict:
    """Preflight by default; confirmation clears only the selected bound stop."""
    root = Path(target_root).expanduser().absolute()
    stop = root / 'runtime/stop-preaudit.flag'
    result = {'schema_version': 1, 'kind': 'preaudit_owner_restart_recovery',
        'status': 'blocked', 'stop_cleared': False, 'write_state': 'not_requested',
        'original_wait_verified': False, 'original_exit_code': None,
        'automation_state': 'unknown', 'approval_started': False,
        'production_acceptance': 'not_verified', 'receipt_file': None}
    material_proof = None
    audit = None
    clear_attempted = False
    try:
        maintenance_runtime.assert_target_environment(root)
        if not production_release._REVISION.fullmatch(expected_revision):
            raise RuntimeError('production_release_revision_invalid')
        if prepare_failure and (user_stop or selected_action is None):
            raise RuntimeError('owner_restart_prepare_failure_selection_required')
        if prepare_failure:
            selected = Path(selected_action).expanduser().absolute()
            material_proof = {'kind': 'preaudit_material_failure_source_only',
                              'target_root': str(root), 'actions': [str(selected)]}
        with maintenance_runtime.target_runtime_leases(root):
            controls = (production_release._prepare_failure_controls(root, automation)
                        if prepare_failure else
                        production_release._stopped_controls(root, automation, user_stop_source_only=user_stop))
            policy = root / 'runtime/control/write-policy.json'
            pinned_policy = production_release._pinned_control(policy, private=False)
            result['automation_state'] = 'PAUSED'
            production_release._source(root, expected_revision, expected_origin, synchronized=True)
            if production_release.candidate_configuration_findings(root, expected_revision):
                raise RuntimeError('production_release_configuration_invalid')
            manifest = controller_handoff.manifest_snapshot(root / 'runtime/control/rolling-controller-manifest.json')
            report = (_read_prepare_failure_original(root, original, manifest, controls)
                      if prepare_failure else
                      _read_original(root, original, manifest, user_stop=user_stop))
            snapshot = (check_live(root) if check_live else maintenance_runtime.read_live_status(
                root, require_reaped=True, source_only_proof=material_proof))
            maintenance_runtime.assert_live_safe(snapshot, source_only_proof=material_proof)
            receipt = snapshot['maintenance_status_file']
            proof_key = 'prepare_failure_recovery_proof' if prepare_failure else 'owner_restart_proof'
            result.update(target_root=str(root), revision=expected_revision,
                original_wait_verified=True, original_exit_code=report['exit_code'],
                original_receipt_file=str(original.file), original_owner_task_id=original.owner_task_id,
                original_tool_session_id=original.tool_session_id, original_invocation_id=original.file.parent.name,
                owner_restart_proof=report[proof_key], current_status_file=receipt,
                current_inputs=snapshot['maintenance_current_inputs'], continuation=snapshot['reconciliation']['continuation'],
                stop_before=production_release._control_proof(controls[0]),
                automation_before=production_release._control_proof(controls[1]))
            if prepare_failure:
                result.update(prepare_failure_recovery=True,
                              selected_recovery_action=str(selected_action))

            def verify_unchanged() -> None:
                current_controls = (production_release._prepare_failure_controls(root, automation)
                                    if prepare_failure else
                                    production_release._stopped_controls(root, automation, user_stop_source_only=user_stop))
                if current_controls != controls:
                    raise RuntimeError('owner_restart_controls_changed')
                if production_release._pinned_control(policy, private=False) != pinned_policy:
                    raise RuntimeError('owner_restart_policy_changed')
                production_release._source(root, expected_revision, expected_origin, synchronized=True)
                current_report = (_read_prepare_failure_original(root, original, manifest, controls)
                                  if prepare_failure else
                                  _read_original(root, original, manifest, user_stop=user_stop))
                if current_report != report:
                    raise RuntimeError('owner_restart_original_changed')
                if maintenance_runtime.read_current_inputs(
                        root, receipt, require_reaped=True, source_only_proof=material_proof
                ) != snapshot['maintenance_current_inputs']:
                    raise RuntimeError('owner_restart_inputs_changed')

            verify_unchanged()
            if not confirmed:
                result['status'] = 'ready'
                return result
            directory = root / 'runtime/control' / (('prepare-failure-recovery-' if prepare_failure
                                                       else 'owner-restart-recovery-') + uuid4().hex)
            directory.mkdir(mode=0o700)
            audit = directory / 'receipt.json'
            result.update(status='validated', receipt_file=str(audit), recorded_at=datetime.now(timezone.utc).isoformat())
            runtime_control.write_control_bytes_atomic(audit, (json.dumps(result, sort_keys=True) + '\n').encode())
            with runtime_control.stop_control_lease(stop):
                verify_unchanged()
                clear_attempted = True
                if not runtime_control._clear_stop_locked(controls[0][1], expected_identity=controls[0][0][:3], target=stop):
                    raise RuntimeError('owner_restart_stop_changed')
                result['stop_cleared'] = True
            if (os.path.lexists(stop) or production_release._pinned_control(automation.file, private=False) != controls[1]):
                raise RuntimeError('owner_restart_controls_changed')
            result['status'] = 'cleared'
            runtime_control.write_control_bytes_atomic(audit, (json.dumps(result, sort_keys=True) + '\n').encode())
            if (os.path.lexists(stop) or production_release._pinned_control(automation.file, private=False) != controls[1]):
                raise RuntimeError('owner_restart_controls_changed')
        return result
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        result.update(status='blocked', error_code=str(error) if isinstance(error, RuntimeError)
            else 'owner_restart_evidence_invalid')
        if clear_attempted and result['stop_cleared'] is not True:
            result['stop_cleared'] = 'unknown'
        observed = production_release._stopped_observation(root, automation)
        result.update(controls_after=observed, automation_state=observed['automation_state'])
        if audit is not None:
            try:
                runtime_control.write_control_bytes_atomic(audit, (json.dumps(result, sort_keys=True) + '\n').encode())
            except (OSError, ValueError, RuntimeError):
                result['audit_finalization'] = 'failed'
        return result


def main(argv: list[str] | None = None, *, check_live: Callable[[Path], dict] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-root', type=Path, required=True)
    parser.add_argument('--expected-revision', required=True)
    parser.add_argument('--expected-origin', default=production_release.ORIGIN)
    parser.add_argument('--automation-file', type=Path, required=True)
    parser.add_argument('--production-project-id', required=True)
    parser.add_argument('--expected-schedule', required=True)
    parser.add_argument('--original-receipt-file', type=Path, required=True)
    parser.add_argument('--original-owner-task-id', required=True)
    parser.add_argument('--original-tool-session-id', required=True)
    parser.add_argument('--confirm-recovery', choices=('YES',))
    parser.add_argument('--resume-user-stop', choices=('YES',))
    parser.add_argument('--prepare-failure-recovery', choices=('YES',))
    parser.add_argument('--recover-action-file', type=Path)
    args = parser.parse_args(argv)
    prepare_failure = args.prepare_failure_recovery == 'YES'
    if prepare_failure and args.recover_action_file is None:
        parser.error('--recover-action-file is required with --prepare-failure-recovery YES')
    result = recover(args.target_root, expected_revision=args.expected_revision, expected_origin=args.expected_origin,
        automation=production_release.ProductionAutomation(args.automation_file, args.production_project_id, args.expected_schedule),
        original=maintenance_runtime.OriginalExecution(args.original_receipt_file, args.original_owner_task_id, args.original_tool_session_id),
        confirmed=args.confirm_recovery == 'YES', check_live=check_live, user_stop=args.resume_user_stop == 'YES',
        prepare_failure=prepare_failure, selected_action=args.recover_action_file)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] in {'ready', 'cleared'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
