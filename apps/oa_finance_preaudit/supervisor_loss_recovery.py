"""Audited stop recovery after a lost OS wait; never starts approval or writes OA."""
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


# The target's installed recovery contract independently selects exactly one
# current unwritten cancellation. Source-maintenance proof alone grants nothing.
_SELECTION_READER = r'''
import hashlib, json, os, sys
from pathlib import Path
from apps.oa_finance_preaudit import approval_guard, reconciliation, flow_engine, production_release
from scripts import finance_preaudit_concurrency_probe as probe
root = Path.cwd().resolve()
if any(not Path(module.__file__).resolve().is_relative_to(root) for module in
        (approval_guard, reconciliation, flow_engine, production_release, probe)):
    raise ValueError('supervisor_loss_recovery_selection_untrusted')
path, digest, selected = sys.argv[1:]
receipt = Path(path)
status_limit = 64 * 1024 * 1024
pinned_status = production_release._pinned_control(receipt, max_bytes=status_limit)
if hashlib.sha256(pinned_status[1]).hexdigest() != digest:
    raise ValueError('supervisor_loss_recovery_status_changed')
snapshot = json.loads(pinned_status[1])
reconciled = snapshot['reconciliation']
flows = root / 'runtime/finance_preaudit_flows'
states = reconciliation.latest_states(flows)
by_key = {(s['workflow_key'], s['case_key'], s['attempt']):s for s in states}
pending = [item for item in reconciled['findings'] if item['category'] == 'pending_not_handled']
selected_state = next((s for s in states if s.get('action_file') == selected), None)
selected_findings = [item for item in reconciled['findings'] if selected_state is not None
    and (item['workflow_key'], item['item_key'], item['attempt']) ==
        (selected_state['workflow_key'], selected_state['case_key'], selected_state['attempt'])]
if (selected_state is None or selected_state.get('attempt') != 1 or len(selected_findings) != 1
        or selected_findings[0]['category'] != 'pending_not_handled'
        or selected_findings[0]['recommendation'] != 'confirmed_no_write_recovery_candidate'
        or not flow_engine.FlowEngine.recoverable_no_write(flows, Path(selected))
        or not probe._continuation_is_ready(reconciled, recovery_action_files=(Path(selected),))):
    raise ValueError('supervisor_loss_recovery_selection_untrusted')
files = {receipt, root / 'runtime/control/write-policy.json'}
for item in pending:
    state = by_key[(item['workflow_key'], item['item_key'], item['attempt'])]
    attempt = flows / state['workflow_key'] / 'cases' / state['case_key'] / ('attempt-' + str(state['attempt']))
    files.update(attempt / name for name in ('action.json','state.json','manual-review-receipt.json',
        'material.json','review-packet.json','writer-boundary.json','execution-receipt.json',
        'review-decision.json','remark.txt','submission-envelope.json','recovery-receipt.json'))
def fingerprints():
    return {str(file):production_release._control_proof(production_release._pinned_control(file, private=False,
        max_bytes=status_limit if file == receipt else 1_048_576))
        if os.path.lexists(file) else None for file in sorted(files)}
before = fingerprints()
if (reconciliation.latest_states(flows) != states or fingerprints() != before
        or production_release._pinned_control(receipt, max_bytes=status_limit) != pinned_status):
    raise ValueError('supervisor_loss_recovery_inputs_changed')
if list(approval_guard.project_official_errors(root)):
    raise ValueError('supervisor_loss_recovery_official_notice')
print(json.dumps({'kind':'preaudit_supervisor_loss_recovery_selection','read_only':True,
    'selected_action':selected,'status_sha256':digest,'attempt':1,'existing_recovery_contract':True,
    'official_history_clear':True,'evidence_files':before}))
'''


def _read_selection(root: Path, receipt: dict, selected: Path) -> dict:
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    result = maintenance_runtime._run_readonly(
        [str(root / '.venv/bin/python'), '-B', '-E', '-c', _SELECTION_READER,
            receipt['path'], receipt['sha256'], str(selected)],
        target_root=root, environment=environment, timeout=60, require_reaped=True)
    if result.returncode or len(result.stdout.encode()) > 1_000_000:
        raise RuntimeError('supervisor_loss_recovery_selection_untrusted')
    proof = json.loads(result.stdout)
    if (not isinstance(proof, dict) or proof.get('selected_action') != str(selected)
            or proof.get('status_sha256') != receipt['sha256'] or proof.get('attempt') != 1
            or proof.get('existing_recovery_contract') is not True
            or proof.get('official_history_clear') is not True or not proof.get('evidence_files')):
        raise RuntimeError('supervisor_loss_recovery_selection_untrusted')
    return proof


def recover(
    target_root: Path, *, expected_revision: str,
    automation: production_release.ProductionAutomation,
    original: maintenance_runtime.OriginalExecution, selected_action: Path,
    confirmed: bool = False, expected_origin: str = production_release.ORIGIN,
    check_live: Callable[[Path], dict] | None = None,
) -> dict:
    """Default to preflight; confirmation only clears the exact audited stop.

    The injectable live reader substitutes external readonly OA connections for
    offline callers. The CLI cannot import cached state or bypass local proofs.
    """
    root = Path(target_root).expanduser().absolute()
    selected = Path(selected_action).expanduser().absolute()
    result = {'schema_version': 1, 'kind': 'preaudit_supervisor_loss_recovery',
        'status': 'blocked', 'stop_cleared': False, 'write_state': 'not_requested',
        'original_wait_verified': False, 'original_exit_code': None,
        'historical_stop_attribution': 'not_verified', 'selected_recovery_action': str(selected),
        'automation_state': 'unknown', 'approval_started': False,
        'production_acceptance': 'not_verified', 'receipt_file': None}
    stop = root / 'runtime/stop-preaudit.flag'
    clear_attempted = False
    audit = None
    try:
        maintenance_runtime.assert_target_environment(root)
        if not production_release._REVISION.fullmatch(expected_revision):
            raise RuntimeError('production_release_revision_invalid')
        with maintenance_runtime.target_runtime_leases(root):
            controls = production_release._stopped_controls(root, automation)
            policy = root / 'runtime/control/write-policy.json'
            pinned_policy = production_release._pinned_control(policy, private=False)
            result['automation_state'] = 'PAUSED'
            production_release._source(root, expected_revision, expected_origin, synchronized=True)
            findings = production_release.candidate_configuration_findings(root, expected_revision)
            if findings:
                raise RuntimeError('production_release_configuration_invalid')
            manifest = controller_handoff.manifest_snapshot(root / 'runtime/control/rolling-controller-manifest.json')
            original_report = maintenance_runtime.read_original_execution(root, original, supervisor_loss_source_only=True)
            maintenance_runtime.assert_original_execution_safe(root, original, original_report, manifest,
                supervisor_loss_source_only=True)
            loss_proof = original_report['source_only_proof']
            snapshot = (check_live(root) if check_live else maintenance_runtime.read_live_status(root,
                require_reaped=True, source_only_proof=loss_proof))
            maintenance_runtime.assert_live_safe(snapshot, source_only_proof=loss_proof)
            if snapshot['maintenance_current_inputs'].get('preserved_stop_failures') != [str(selected)]:
                raise RuntimeError('supervisor_loss_recovery_selection_untrusted')
            receipt = snapshot['maintenance_status_file']
            selection = _read_selection(root, receipt, selected)
            result.update(target_root=str(root), revision=expected_revision,
                original_receipt_file=str(original.file), original_owner_task_id=original.owner_task_id,
                original_tool_session_id=original.tool_session_id, original_invocation_id=original.file.parent.name,
                original_run_id=original_report['run_id'], original_controller_instance_id=original_report['controller_instance_id'],
                original_evidence=loss_proof, current_status_file=receipt, current_inputs=selection,
                continuation=snapshot['reconciliation']['continuation'],
                policy_before=production_release._control_proof(pinned_policy),
                stop_before=production_release._control_proof(controls[0]),
                automation_before=production_release._control_proof(controls[1]))

            def verify_unchanged() -> None:
                if production_release._stopped_controls(root, automation) != controls:
                    raise RuntimeError('supervisor_loss_recovery_controls_changed')
                if production_release._pinned_control(policy, private=False) != pinned_policy:
                    raise RuntimeError('supervisor_loss_recovery_policy_changed')
                production_release._source(root, expected_revision, expected_origin, synchronized=True)
                observed = maintenance_runtime.read_original_execution(root, original, supervisor_loss_source_only=True)
                maintenance_runtime.assert_original_execution_safe(root, original, observed, manifest,
                    supervisor_loss_source_only=True)
                if observed['source_only_proof'] != loss_proof:
                    raise RuntimeError('supervisor_loss_recovery_original_changed')
                current = maintenance_runtime.read_current_inputs(root, receipt,
                    require_reaped=True, source_only_proof=loss_proof)
                if (current != snapshot['maintenance_current_inputs']
                        or _read_selection(root, receipt, selected) != selection):
                    raise RuntimeError('supervisor_loss_recovery_inputs_changed')

            verify_unchanged()
            if not confirmed:
                result['status'] = 'ready'
                return result
            directory = root / 'runtime/control' / ('supervisor-loss-recovery-' + uuid4().hex)
            directory.mkdir(mode=0o700)
            audit = directory / 'receipt.json'
            result.update(status='validated', receipt_file=str(audit), recorded_at=datetime.now(timezone.utc).isoformat())
            runtime_control.write_control_bytes_atomic(audit, (json.dumps(result, sort_keys=True) + '\n').encode())
            with runtime_control.stop_control_lease(stop):
                verify_unchanged()
                clear_attempted = True
                if not runtime_control._clear_stop_locked(controls[0][1],
                        expected_identity=controls[0][0][:3], target=stop):
                    raise RuntimeError('supervisor_loss_recovery_stop_changed')
                result['stop_cleared'] = True
            if (os.path.lexists(stop) or production_release._pinned_control(automation.file, private=False) != controls[1]):
                raise RuntimeError('supervisor_loss_recovery_controls_changed')
            result['status'] = 'cleared'
            runtime_control.write_control_bytes_atomic(audit, (json.dumps(result, sort_keys=True) + '\n').encode())
            if (os.path.lexists(stop) or production_release._pinned_control(automation.file, private=False) != controls[1]):
                raise RuntimeError('supervisor_loss_recovery_controls_changed')
        return result
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        result.update(status='blocked', error_code=str(error) if isinstance(error, RuntimeError)
            else 'supervisor_loss_recovery_evidence_invalid')
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
    parser.add_argument('--recover-action-file', type=Path, required=True)
    parser.add_argument('--confirm-recovery', choices=('YES',))
    args = parser.parse_args(argv)
    result = recover(args.target_root, expected_revision=args.expected_revision,
        expected_origin=args.expected_origin,
        automation=production_release.ProductionAutomation(args.automation_file, args.production_project_id, args.expected_schedule),
        original=maintenance_runtime.OriginalExecution(args.original_receipt_file, args.original_owner_task_id, args.original_tool_session_id),
        selected_action=args.recover_action_file, confirmed=args.confirm_recovery == 'YES', check_live=check_live)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] in {'ready', 'cleared'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
