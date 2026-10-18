"""Recover an audited cycle stop left by a sealed App shutdown, without OA writes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets

from apps.oa_approval import runtime_control
from . import maintenance_runtime, production_release

BLOCKED = 'shutdown_stop_recovery_blocked'

_BOUND_READER = maintenance_runtime._ORIGINAL_EXECUTION_READER + r'''
import os, re, shlex
from datetime import datetime
from apps.oa_finance_preaudit import host_chat_review as host, app_review_shutdown as shutdown
from apps.oa_finance_preaudit import approval_guard as guard, approval_lifecycle as lifecycle, controller_handoff
if any(not Path(module.__file__).resolve().is_relative_to(root) for module in
       (host, shutdown, guard, lifecycle, controller_handoff)):
    raise ValueError('shutdown_stop_source_untrusted')
saved_execution=json.loads(finance_preaudit_run._read_evidence(Path(path)))
value=host._registry(owner,live=False,run_id=report.get('run_id'))
plan=value['shutdown']; receipt=plan['receipt']; mirror=controller_handoff._read_manifest()
if (plan['status']!='verified' or plan['reason'] not in {'reviewer_failed','parent_lost','cancelled'}
    or value['state']!='cancelled' or value['close_reason']!=plan['reason']
    or value['original_execution']!={'receipt_file':path,'owner_task_id':owner,
        'invocation_id':Path(path).parent.name,'tool_session_id':session}
    or report.get('terminal') is not True or report.get('children_reaped') is not True
    or report.get('controller_state')!='failed' or type(report.get('exit_code')) is not int
    or report['exit_code']==0 or mirror is None or mirror['state']!='failed'
    or mirror['children_reaped'] is not True
    or mirror!=json.loads(finance_preaudit_run._read_evidence(Path(path).with_name('controller-manifest.json')))
    or any(report.get(k)!=value.get(k) or mirror.get(k)!=value.get(k)
           for k in ('run_id','controller_instance_id'))):
    raise ValueError('shutdown_stop_execution_untrusted')
source=Path(value['reviewers'][0]['spawn_source']['session_file'])
stops=[]
for reviewer,saved in zip(value['reviewers'],receipt['interrupt_sources'],strict=True):
    anchor,proof=shutdown._source(value,owner,source,saved['line'],earliest=plan['started_at'])
    output=proof.pop('output')
    if (host._tool(anchor,'interrupt_agent')!={'target':reviewer['agent_path']}
        or set(output)!={'previous_status'} or proof!=saved):
        raise ValueError('shutdown_stop_interrupt_untrusted')
    host._native_agent_status(output['previous_status']); stops.append(proof)
anchor,proof=shutdown._source(value,owner,source,receipt['children_source']['line'],earliest=plan['started_at'])
host._tool(anchor,'list_agents'); output=proof.pop('output')
if output.get('isError') or output.get('error') or proof['line']<=max(p['output_line'] for p in stops):
    raise ValueError('shutdown_stop_children_untrusted')
statuses=[]
for reviewer in value['reviewers']:
    matches=[a for a in output['agents'] if a.get('agent_name')==reviewer['agent_path']]
    if len(matches)!=1 or host._native_agent_status(matches[0]['agent_status']) not in {'idle','completed','interrupted'}:
        raise ValueError('shutdown_stop_children_untrusted')
    statuses.append({'agent_name':reviewer['agent_path'],'agent_status':host._native_agent_status(matches[0]['agent_status'])})
proof['reviewers']=statuses
if proof!=receipt['children_source']: raise ValueError('shutdown_stop_children_untrusted')
anchor,proof=shutdown._source(value,owner,source,receipt['driver_wait_source']['line'],earliest=value['registered_at'])
wait=shutdown._wait_result(anchor,proof.pop('output'),session)
if proof!=receipt['driver_wait_source'] or wait['exit_code']!=report['exit_code']:
    raise ValueError('shutdown_stop_wait_untrusted')
expected={'kind':'preaudit_app_shutdown','status':'verified','owner_task_id':owner,
    'run_id':value['run_id'],'controller_instance_id':value['controller_instance_id'],
    'reason':plan['reason'],'exchange_state':'cancelled','interrupt_sources':stops,
    'children_source':receipt['children_source'],'driver_wait_source':proof,
    'driver_exit_code':report['exit_code'],'children_reaped':True,'writer_active':False,
    'execution_sha256':host.hashlib.sha256(host._read(Path(path))[0]).hexdigest()}
if receipt!=expected: raise ValueError('shutdown_stop_receipt_untrusted')
native,begin=host._native_source(source,owner,int(sys.argv[4]),output=True)
anchor=native['anchor']; code=anchor['payload'].get('input','')
if anchor['payload'].get('name') not in {'exec','functions.exec'}:
    raise ValueError('shutdown_stop_begin_untrusted')
commands=[]
remaining=code
while remaining.strip():
    match=re.match(r'\s*text\(await tools\.exec_command\((\{[^\n]+\})\)\);',remaining)
    if match is None or len(commands)>=2:
        raise ValueError('shutdown_stop_begin_untrusted')
    args=shutdown._literal_arguments(match[1],{'cmd','max_output_tokens'})
    commands.append(shlex.split(args['cmd']))
    remaining=remaining[match.end():]
expected_commands=[['.venv/bin/python','-B','-m','apps.oa_finance_preaudit.host_chat_review',
    '--operation','begin-shutdown','--owner-task-id',owner,'--reason',plan['reason']]]
if len(commands)==2:
    expected_commands.append(['.venv/bin/python','-m','scripts.finance_preaudit_run',
        '--owner-task-id',owner,'--receipt-file',path,'--invocation-id',Path(path).parent.name,
        '--tool-session-id',session,'--retrieve-result'])
if commands!=expected_commands:
    raise ValueError('shutdown_stop_begin_untrusted')
results=[]
def visit(item):
    if isinstance(item,str):
        try: visit(json.loads(item))
        except ValueError: pass
    elif isinstance(item,list):
        for part in item: visit(part)
    elif isinstance(item,dict):
        if item.get('isError') or item.get('error'):
            raise ValueError('shutdown_stop_begin_untrusted')
        if 'output' in item and ('exit_code' in item or 'session_id' in item): results.append(item)
        else:
            for part in item.values(): visit(part)
visit(begin['output'])
if (len(results)!=len(commands) or results[0].get('exit_code')!=0
    or any(type(r.get('exit_code')) is not int or r.get('session_id') is not None for r in results)):
    raise ValueError('shutdown_stop_begin_untrusted')
if len(results)==2:
    retrieved=json.loads(results[1]['output'])
    if (results[1]['exit_code']!=report['exit_code']
        or any(retrieved.get(k)!=report.get(k) for k in ('kind','owner_task_id','invocation_id',
            'tool_session_id','run_id','controller_instance_id','terminal','exit_code','children_reaped'))):
        raise ValueError('shutdown_stop_begin_untrusted')
started=json.loads(results[0]['output'])
if started!={k:plan[k] for k in ('status','reason','started_at','interrupt_targets','original_tool_session_id')}|{'status':'pending'}:
    raise ValueError('shutdown_stop_begin_untrusted')
stop=root/'runtime/stop-preaudit.flag'; info=stop.lstat()
modified=info.st_mtime_ns/1e9
call_at=datetime.fromisoformat(anchor['timestamp'].replace('Z','+00:00')).timestamp()
native_output,_=host._native_source(source,owner,begin['output_line'])
output_at=datetime.fromisoformat(native_output['anchor']['timestamp'].replace('Z','+00:00')).timestamp()
if not datetime.fromisoformat(saved_execution['completed_at']).timestamp()<=call_at<=plan['started_at']<=modified<=output_at<=plan['verified_at']:
    raise ValueError('shutdown_stop_attribution_untrusted')
for key in ('controller_process','supervisor_process'):
    identity=json.loads(finance_preaudit_run._read_evidence(Path(path)))[key]
    if not guard._valid_identity(identity) or guard.process_identity(identity['pid']) is not None or guard.group_alive(identity['pgid']):
        raise ValueError('shutdown_stop_process_active')
progress=lifecycle._progress(mirror)
if (not progress or progress['project_root']!=str(root) or progress['writer_active'] is not False
    or not lifecycle.readonly_reaped(mirror) or not guard.models_reaped()):
    raise ValueError('shutdown_stop_process_active')
for index in range(guard.MAX_MODEL_WORKERS):
    slot=guard._slot(index)
    if slot and slot.get('pid') is not None:
        current=guard.process_identity(slot['pid'])
        identity=slot.get('process')
        if (guard.group_alive(slot['pid']) or current is not None
            and (not guard._valid_identity(identity) or current==identity)):
            raise ValueError('shutdown_stop_process_active')
cooldown=guard.cooldown()
if cooldown and host.time.time()<cooldown['blocked_until']: raise ValueError('shutdown_stop_cooldown')
files=[host.registry_file(owner,report.get('run_id')),Path(path),Path(path).with_name('tool-session.json'),
       Path(path).with_name('controller-manifest.json'),controller_handoff.manifest_file(),
       lifecycle.run_directory(mirror['run_id'])/'progress.json',
       *(guard._slot_path(i) for i in range(guard.MAX_MODEL_WORKERS)),
       *lifecycle.run_directory(mirror['run_id']).glob('worker-*.json'),
       *lifecycle.run_directory(mirror['run_id']).glob('pool-*.json')]
proofs={}
for p in files:
    if not p.exists(): continue
    before=p.lstat()
    identity=lambda s:[getattr(s,k) for k in ('st_dev','st_ino','st_mtime_ns','st_ctime_ns','st_mode','st_uid','st_size')]
    digest=finance_preaudit_run._log_evidence(p)[0]
    if identity(before)!=identity(p.lstat()) or before.st_uid!=os.getuid() or before.st_mode&0o077:
        raise ValueError('shutdown_stop_evidence_changed')
    proofs[str(p)]={**digest,'identity':identity(before)}
print(json.dumps({'execution':report,'shutdown':receipt,'begin_source':{k:v for k,v in begin.items() if k!='output'},
    'stop_mtime_ns':info.st_mtime_ns,'evidence':proofs}))
'''


def _bound(target: Path, original: maintenance_runtime.OriginalExecution, begin_line: int) -> dict:
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    result = maintenance_runtime._run_readonly(
        [str(target / '.venv/bin/python'), '-B', '-E', '-c', _BOUND_READER,
         str(original.file), original.owner_task_id, original.tool_session_id, str(begin_line)],
        target_root=target, environment=environment, timeout=60, require_reaped=True)
    if result.returncode or len(result.stdout.encode()) > 1_000_000:
        raise ValueError(BLOCKED)
    return json.loads(result.stdout)


def _controls(target: Path, automation: production_release.ProductionAutomation) -> tuple:
    if os.environ.get('OA_FINANCE_PREAUDIT_STOP_REQUESTED', '').strip().lower() in {'1','true','yes','y','on'}:
        raise ValueError(BLOCKED)
    stop = production_release._pinned_control(target / 'runtime/stop-preaudit.flag')
    if stop[1] != b'stop_requested:controller_cycle_stop\n' or automation.findings(target):
        raise ValueError(BLOCKED)
    return stop, production_release._paused_cron(automation)


def _material_failure_source_only(target: Path, actions: tuple[Path, ...] | None) -> dict | None:
    if actions is None:
        return None
    target = target.absolute()
    paths = [Path(action).expanduser() for action in actions]
    if (not paths or any(not path.is_absolute() for path in paths)
            or len({str(path) for path in paths}) != len(paths)
            or any(not path.is_relative_to(target) for path in paths)):
        raise ValueError(BLOCKED)
    return {'kind': 'preaudit_material_failure_source_only', 'target_root': str(target),
            'actions': [str(path) for path in paths]}


def recover(*, target_root: Path, expected_revision: str, original: maintenance_runtime.OriginalExecution,
            automation: production_release.ProductionAutomation, begin_line: int,
            confirmed: bool = False, material_failure_actions: tuple[Path, ...] | None = None) -> dict:
    target = Path(target_root).expanduser().absolute()
    maintenance_runtime.assert_target_environment(target)
    candidate = Path(__file__).resolve().parents[2]
    if (candidate == target or production_release._git(candidate, 'rev-parse', 'HEAD') != expected_revision
            or production_release._git(candidate, 'rev-parse', 'origin/main') != expected_revision
            or production_release._git(candidate, 'branch', '--show-current') != 'main'
            or production_release._git(candidate, 'status', '--porcelain')):
        raise ValueError(BLOCKED)
    marker = target / 'runtime/stop-preaudit.flag'
    audit = None
    result = {'kind':'preaudit_shutdown_stop_recovery','status':'blocked','stop_cleared':False,
              'write_state':'not_requested','approval_started':False,'production_acceptance':'not_verified'}
    try:
        source_only_proof = _material_failure_source_only(target, material_failure_actions)
        result['material_failure_source_only'] = source_only_proof is not None
        if source_only_proof is not None:
            result['material_failure_actions'] = source_only_proof['actions']
        with maintenance_runtime.target_runtime_leases(target):
            controls = _controls(target, automation)
            revision = production_release._source(target, expected_revision, production_release.ORIGIN)
            bound = _bound(target, original, begin_line)
            if revision != bound['execution']['code_revision']:
                raise ValueError(BLOCKED)
            manifest = runtime_control.read_control_bytes(target / 'runtime/control/rolling-controller-manifest.json')
            maintenance_runtime.assert_original_execution_safe(target, original, bound['execution'], manifest)
            snapshot = maintenance_runtime.read_live_status(
                target, require_reaped=True, source_only_proof=source_only_proof,
            )
            maintenance_runtime.assert_live_safe(snapshot, source_only_proof=source_only_proof)
            result.update(target_root=str(target),candidate_revision=expected_revision,target_revision=revision,
                          bound_evidence=bound,current_status_file=snapshot['maintenance_status_file'],
                          current_inputs=snapshot['maintenance_current_inputs'],
                          stop_before=production_release._control_proof(controls[0]),automation_state='PAUSED')

            def verify() -> None:
                if (_controls(target, automation) != controls or _bound(target, original, begin_line) != bound
                        or production_release._source(target, expected_revision, production_release.ORIGIN) != revision
                        or maintenance_runtime.read_current_inputs(
                            target, snapshot['maintenance_status_file'], require_reaped=True,
                            source_only_proof=source_only_proof,
                        ) != snapshot['maintenance_current_inputs']):
                    raise ValueError(BLOCKED)
                current_inputs = maintenance_runtime.read_current_inputs(
                    target, snapshot['maintenance_status_file'], require_reaped=True,
                    source_only_proof=source_only_proof,
                )
                maintenance_runtime.assert_live_safe(
                    {**snapshot, 'maintenance_current_inputs': current_inputs},
                    source_only_proof=source_only_proof,
                )

            verify()
            if not confirmed:
                return {**result,'status':'ready'}
            audit = target / 'runtime/control' / ('shutdown-stop-recovery-' + secrets.token_hex(16) + '.json')
            result.update(status='validated',receipt_file=str(audit),recorded_at=datetime.now(timezone.utc).isoformat())
            def record() -> None:
                raw = (json.dumps(result,sort_keys=True)+'\n').encode()
                runtime_control.write_control_bytes_atomic(audit,raw)
                if production_release._pinned_control(audit)[1] != raw:
                    raise ValueError(BLOCKED)
            record()
            attempted = False
            try:
                with runtime_control.stop_control_lease(marker):
                    verify()
                    attempted = True
                    if not runtime_control._clear_stop_locked(controls[0][1],expected_identity=controls[0][0][:3],target=marker):
                        raise ValueError(BLOCKED)
                    result['stop_cleared'] = True
                if os.path.lexists(marker) or production_release._paused_cron(automation) != controls[1]:
                    raise ValueError(BLOCKED)
                result['status'] = 'cleared'
                record()
            except (OSError, ValueError, RuntimeError):
                if attempted and result['stop_cleared'] is not True:
                    result['stop_cleared'] = 'unknown'
                result['status'] = 'maintenance_completion_failed'
                try:
                    record()
                except (OSError, ValueError, RuntimeError):
                    result['audit_finalization'] = 'failed'
            return result
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        result.update(status='maintenance_completion_failed' if result['stop_cleared'] is not False else 'blocked',
                      error_code=BLOCKED)
        if audit is not None:
            try:
                runtime_control.write_control_bytes_atomic(audit,(json.dumps(result,sort_keys=True)+'\n').encode())
            except (OSError, ValueError, RuntimeError):
                result['audit_finalization'] = 'failed'
        return result



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-root',type=Path,required=True)
    parser.add_argument('--expected-candidate-revision',required=True)
    parser.add_argument('--original-receipt-file',type=Path,required=True)
    parser.add_argument('--original-owner-task-id',required=True)
    parser.add_argument('--original-tool-session-id',required=True)
    parser.add_argument('--begin-shutdown-line',type=int,required=True)
    parser.add_argument('--automation-file',type=Path,required=True)
    parser.add_argument('--production-project-id',required=True)
    parser.add_argument('--expected-schedule',required=True)
    parser.add_argument('--preserve-material-failure', type=Path, action='append', default=None)
    parser.add_argument('--confirm-recovery',choices=('YES',))
    args = parser.parse_args(argv)
    try:
        result = recover(target_root=args.target_root,expected_revision=args.expected_candidate_revision,
            original=maintenance_runtime.OriginalExecution(args.original_receipt_file,args.original_owner_task_id,args.original_tool_session_id),
            automation=production_release.ProductionAutomation(args.automation_file,args.production_project_id,args.expected_schedule),
            begin_line=args.begin_shutdown_line,confirmed=args.confirm_recovery=='YES',
            material_failure_actions=(tuple(args.preserve_material_failure)
                if args.preserve_material_failure is not None else None))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        result = {'status':'blocked','error_code':BLOCKED,'stop_cleared':False,'write_state':'not_requested'}
    print(json.dumps(result))
    return 0 if result['status'] in {'ready','cleared'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
