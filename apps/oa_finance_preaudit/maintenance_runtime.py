"""Target-local leases and verified read-only evidence for production maintenance."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from typing import Iterator

from apps.oa_approval import runtime_control
from . import controller_handoff


@dataclass(frozen=True)
class OriginalExecution:
    file: Path
    owner_task_id: str
    tool_session_id: str


def _reap_readonly_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)
    deadline = time.monotonic() + 5
    while True:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('maintenance_readonly_children_unreaped')
        time.sleep(.01)


def _run_readonly(arguments: list[str], *, target_root: Path, environment: dict,
                  timeout: int, require_reaped: bool = False) -> subprocess.CompletedProcess:
    if not require_reaped:
        return subprocess.run(arguments, cwd=target_root, env=environment, capture_output=True,
                              text=True, check=False, timeout=timeout)
    with subprocess.Popen(arguments, cwd=target_root, env=environment, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
            raise RuntimeError('maintenance_readonly_children_unreaped')
        except BaseException as error:
            try:
                _reap_readonly_group(process)
            except BaseException as cleanup_error:
                raise error from cleanup_error
            raise


_ORIGINAL_EXECUTION_READER = '''
import json, sys
from pathlib import Path
from scripts import finance_preaudit_run
root = Path.cwd().resolve()
if not Path(finance_preaudit_run.__file__).resolve().is_relative_to(root):
    raise ValueError('maintenance_original_execution_untrusted')
path, owner, session = sys.argv[1:4]
report = finance_preaudit_run.read_execution(Path(path), owner_task_id=owner,
    invocation_id=Path(path).parent.name, tool_session_id=session)
'''


_HISTORICAL_STOP_VALIDATOR = r'''
def historical_stopped_collection(attempt, state):
    if state.get('execution_state') != 'needs_admin' or state.get('failure_detail_code') != 'oa_run_stopped':
        return False
    receipt = item_failure.verified_receipt(attempt, state)
    return (receipt is not None and receipt['scope'] == 'controller'
        and item_failure.material_failure_without_write(attempt, state)
        and all(state.get(key) is None for key in ('material_file', 'review_packet_file',
            'material_fingerprint', 'evidence_fingerprint'))
        and not any(os.path.lexists(attempt / name) for name in ('material.json', 'review-packet.json')))
'''


_EVIDENCE_FINGERPRINT_READER = r'''
import os, stat
def fingerprint(file):
    try:
        before = file.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022:
        raise ValueError('maintenance_original_execution_untrusted')
    identity = lambda info: [getattr(info, name) for name in
        ('st_dev','st_ino','st_mtime_ns','st_ctime_ns','st_mode','st_uid','st_size')]
    digest, _ = finance_preaudit_run._log_evidence(file)
    if identity(before) != identity(file.lstat()):
        raise ValueError('maintenance_original_execution_untrusted')
    return {**digest, 'identity':identity(before)}
'''


_SUPERVISOR_LOSS_READER = _HISTORICAL_STOP_VALIDATOR + _EVIDENCE_FINGERPRINT_READER + r'''
import os, stat
from apps.oa_finance_preaudit import approval_guard as guard, approval_lifecycle as lifecycle, item_failure, reconciliation
if any(not Path(module.__file__).resolve().is_relative_to(root) for module in (guard, lifecycle, item_failure, reconciliation)):
    raise ValueError('maintenance_original_execution_untrusted')
path = Path(path)
saved = json.loads(finance_preaudit_run._read_evidence(path))
mirror = json.loads(finance_preaudit_run._read_evidence(path.with_name('controller-manifest.json')))
rolling = root / 'runtime/control/rolling-controller-manifest.json'
if (saved.get('state') != 'waiting' or saved.get('exit_code') is not None
        or saved.get('completed_at') is not None or saved.get('result') is not None
        or type(saved.get('supervisor_pid')) is not int or saved['supervisor_pid'] < 1
        or guard.process_identity(saved['supervisor_pid']) is not None
        or mirror != json.loads(finance_preaudit_run._read_evidence(rolling))
        or mirror.get('state') != 'failed' or mirror.get('children_reaped') is not True):
    raise ValueError('maintenance_original_execution_untrusted')
progress = lifecycle._progress(mirror)
if (progress is None or Path(progress['project_root']).resolve() != root
        or progress['writer_active'] is not False
        or guard.process_identity(mirror['controller_pid']) is not None
        or guard.group_alive(mirror['controller_pid'])
        or not lifecycle.readonly_reaped(mirror) or not guard.models_reaped()
        or guard.cooldown() is not None):
    raise ValueError('maintenance_original_process_active')
slots = [guard._slot(index) for index in range(guard.MAX_MODEL_WORKERS)]
if any(slot and slot.get('pid') is not None and
        (guard.process_identity(slot['pid']) is not None or guard.group_alive(slot['pid'])) for slot in slots):
    raise ValueError('maintenance_original_process_active')
run = lifecycle.run_directory(mirror['run_id'])

def historical_actions():
    flows = root / 'runtime/finance_preaudit_flows'
    actions = []
    for state in reconciliation.latest_states(flows):
        attempt = flows / state['workflow_key'] / 'cases' / state['case_key'] / ('attempt-' + str(state['attempt']))
        if historical_stopped_collection(attempt, state):
            actions.append(str(attempt / 'action.json'))
    return sorted(actions)

historical = historical_actions()

def evidence_paths():
    return sorted({path, path.with_name('tool-session.json'), path.with_name('controller-manifest.json'),
        path.with_name('stdout.log'), path.with_name('stderr.log'), rolling, run/'progress.json',
        *run.glob('pool-*.json'), *run.glob('worker-*.json'),
        *(guard._slot_path(index) for index in range(guard.MAX_MODEL_WORKERS)),
        *(Path(action).with_name(name) for action in [*progress['actions'], *historical]
            for name in ('action.json', 'state.json', 'manual-review-receipt.json'))})

files = evidence_paths()
fingerprints = {str(file):fingerprint(file) for file in files}
required = (path, path.with_name('tool-session.json'), path.with_name('controller-manifest.json'),
    path.with_name('stdout.log'), path.with_name('stderr.log'), rolling, run/'progress.json')
if (any(fingerprints[str(file)] is None for file in required)
        or files != evidence_paths()
        or fingerprints != {str(file):fingerprint(file) for file in files}
        or saved != json.loads(finance_preaudit_run._read_evidence(path))
        or mirror != json.loads(finance_preaudit_run._read_evidence(path.with_name('controller-manifest.json')))
        or mirror != json.loads(finance_preaudit_run._read_evidence(rolling))
        or progress != lifecycle._progress(mirror)
        or historical != historical_actions()
        or report != finance_preaudit_run.read_execution(path, owner_task_id=owner,
            invocation_id=path.parent.name, tool_session_id=session)):
    raise ValueError('maintenance_original_execution_untrusted')
report['source_only_proof'] = {'kind':'preaudit_supervisor_loss_source_only','read_only':True,
    'original_wait_verified':False,'log_evidence_source':'observed_after_supervisor_loss',
    'target_root':str(root),'run_id':mirror['run_id'],
    'controller_instance_id':mirror['controller_instance_id'],
    'readonly_reaped':True,'models_reaped':True,'supervisor_absent':True,'cooldown_clear':True,
    'actions':progress['actions'],'historical_stopped_actions':historical,
    'historical_stop_attribution':'not_verified','evidence_files':fingerprints}
'''


_OFFICIAL_STOP_READER = _EVIDENCE_FINGERPRINT_READER + r'''
from apps.oa_finance_preaudit import approval_guard as guard, approval_lifecycle as lifecycle, item_failure, reconciliation, review_interruption
if any(not Path(module.__file__).resolve().is_relative_to(root)
        for module in (guard, lifecycle, item_failure, reconciliation, review_interruption)):
    raise ValueError('maintenance_original_execution_untrusted')
path = Path(path)
saved = json.loads(finance_preaudit_run._read_evidence(path))
rolling = root / 'runtime/control/rolling-controller-manifest.json'
mirror = json.loads(finance_preaudit_run._read_evidence(path.with_name('controller-manifest.json')))
if (report.get('state') != 'exited' or report.get('terminal') is not True
        or type(report.get('exit_code')) is not int or report['exit_code'] == 0
        or mirror != json.loads(finance_preaudit_run._read_evidence(rolling))
        or mirror.get('state') != 'failed' or mirror.get('children_reaped') is not True
        or type(saved.get('supervisor_pid')) is not int
        or guard.process_identity(saved['supervisor_pid']) is not None):
    raise ValueError('maintenance_original_execution_untrusted')
progress = lifecycle._progress(mirror)
cooldown = guard.cooldown()
if (progress is None or not progress['actions'] or Path(progress['project_root']).resolve() != root
        or progress['writer_active'] is not False
        or guard.process_identity(mirror['controller_pid']) is not None or guard.group_alive(mirror['controller_pid'])
        or not lifecycle.readonly_reaped(mirror) or not guard.models_reaped()
        or cooldown is None or cooldown['run_id'] not in (None, mirror['run_id'])):
    raise ValueError('maintenance_original_process_active')
slots = [guard._slot(index) for index in range(guard.MAX_MODEL_WORKERS)]
if any(slot and slot.get('pid') is not None and
        (guard.process_identity(slot['pid']) is not None or guard.group_alive(slot['pid'])) for slot in slots):
    raise ValueError('maintenance_original_process_active')
closed = review_interruption.closed_run(mirror['run_id'], Path(progress['actions'][0]))
original_cooldown = closed['cooldown']
if (closed['cause'] != guard.STOP_REASON or not isinstance(original_cooldown, dict)
        or original_cooldown.get('run_id') != mirror['run_id']
        or (cooldown['run_id'] is not None and original_cooldown != cooldown)
        or closed['controller_instance_id'] != mirror['controller_instance_id']
        or closed['controller'] != progress['controller'] or closed['project_root'] != str(root)
        or closed['actions'] != progress['actions']
        or not original_cooldown['triggered_at'] <= closed['closed_at'] <= finance_preaudit_run._timestamp(saved['completed_at']).timestamp()):
    raise ValueError('maintenance_official_stop_untrusted')

def project_notices():
    if cooldown['run_id'] is not None:
        return []
    import hashlib
    completed = finance_preaudit_run._timestamp(saved['completed_at']).timestamp()
    if cooldown['triggered_at'] < completed:
        raise ValueError('maintenance_project_notice_untrusted')
    notices = [{'source_sha256':hashlib.sha256(source.encode()).hexdigest(),
                'notice_code':code,'occurred_at':stamp}
               for code, source, stamp in guard.project_official_errors(root)
               if hashlib.sha256(source.encode()).hexdigest() in cooldown['sources']]
    if (len(notices) != len(cooldown['sources'])
            or {notice['source_sha256'] for notice in notices} != set(cooldown['sources'])
            or any(not cooldown['triggered_at'] <= notice['occurred_at'] < cooldown['blocked_until'] for notice in notices)
            or not any(notice['occurred_at'] == cooldown['triggered_at']
                       and notice['notice_code'] == cooldown['notice_code'] for notice in notices)):
        raise ValueError('maintenance_project_notice_untrusted')
    return notices

notices = project_notices()
run = lifecycle.run_directory(mirror['run_id'])
flows = root / 'runtime/finance_preaudit_flows'

def interrupted_actions():
    actions = []
    for state in reconciliation.latest_states(flows):
        if state.get('reason') != review_interruption.REASON:
            continue
        attempt = flows / state['workflow_key'] / 'cases' / state['case_key'] / ('attempt-' + str(state['attempt']))
        receipt = guard.read(attempt / 'review-interruption.json')
        action = str(attempt / 'action.json')
        if (receipt is None or receipt.get('run_id') != mirror['run_id']
                or receipt.get('controller_instance_id') != mirror['controller_instance_id']
                or action not in progress['actions'] or not item_failure.isolated(attempt, state)):
            raise ValueError('maintenance_official_stop_untrusted')
        actions.append(action)
    return sorted(actions)

interrupted = interrupted_actions()
def evidence_paths():
    return sorted({path, path.with_name('tool-session.json'), path.with_name('controller-manifest.json'),
        path.with_name('stdout.log'), path.with_name('stderr.log'), rolling, run/'progress.json', run/'closed-readonly.json',
        guard.directory()/'cooldown.json', *run.glob('pool-*.json'), *run.glob('worker-*.json'),
        *(guard._slot_path(index) for index in range(guard.MAX_MODEL_WORKERS)),
        *(Path(action).with_name(name) for action in progress['actions'] for name in
            (*item_failure._ARTIFACTS,'manual-review-receipt.json','review-interruption.json',
             'interrupted-action.json','interrupted-state.json')),
        *(file for action in progress['actions'] for pattern in ('review-notice-*.json','review-process-*.json')
          for file in Path(action).parent.glob(pattern))})
files = evidence_paths()
fingerprints = {str(file):fingerprint(file) for file in files}
required = (path, path.with_name('tool-session.json'), path.with_name('controller-manifest.json'),
    path.with_name('stdout.log'), path.with_name('stderr.log'), rolling, run/'progress.json', run/'closed-readonly.json',
    guard.directory()/'cooldown.json')
if (any(fingerprints[str(file)] is None for file in required)
        or files != evidence_paths() or fingerprints != {str(file):fingerprint(file) for file in files}
        or cooldown != guard.cooldown() or progress != lifecycle._progress(mirror)
        or notices != project_notices()
        or closed != review_interruption.closed_run(mirror['run_id'], Path(progress['actions'][0]))
        or interrupted != interrupted_actions()
        or report != finance_preaudit_run.read_execution(path,owner_task_id=owner,invocation_id=path.parent.name,tool_session_id=session)):
    raise ValueError('maintenance_original_execution_untrusted')
report['official_stop_proof'] = {'kind':'preaudit_official_stop_source_only','read_only':True,
    'original_wait_verified':True,'target_root':str(root),'run_id':mirror['run_id'],
    'controller_instance_id':mirror['controller_instance_id'],'readonly_reaped':True,'models_reaped':True,
    'supervisor_absent':True,'cooldown':cooldown,'actions':progress['actions'],
    'cooldown_attribution':'project_notice' if cooldown['run_id'] is None else 'approval_run',
    'project_notices':notices,
    'interrupted_actions':interrupted,'evidence_files':fingerprints}
'''


def read_original_execution(target_root: Path, original: OriginalExecution, *,
                            supervisor_loss_source_only: bool = False, official_stop_source_only: bool = False) -> dict:
    """Read the original supervised invocation using target-owned validators."""
    target_root = Path(target_root).absolute()
    assert_target_environment(target_root)
    interpreter = target_root / '.venv/bin/python'
    if (target_root / '.venv').is_symlink() or not interpreter.is_file():
        raise RuntimeError('maintenance_interpreter_untrusted')
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    try:
        if supervisor_loss_source_only and official_stop_source_only:
            raise RuntimeError('maintenance_source_only_modes_conflict')
        reader = _ORIGINAL_EXECUTION_READER + (_SUPERVISOR_LOSS_READER if supervisor_loss_source_only else '')
        if official_stop_source_only:
            reader += _OFFICIAL_STOP_READER
        reader += '\nprint(json.dumps(report))\n'
        result = _run_readonly([str(interpreter), '-B', '-E', '-c', reader,
            str(original.file), original.owner_task_id, original.tool_session_id], target_root=target_root,
            environment=environment, timeout=60, require_reaped=True)
        if result.returncode or len(result.stdout.encode()) > 1_000_000:
            raise RuntimeError('maintenance_original_execution_untrusted')
        report = json.loads(result.stdout)
        if not isinstance(report, dict):
            raise RuntimeError('maintenance_original_execution_untrusted')
        return report
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RuntimeError('maintenance_original_execution_untrusted') from error


def assert_original_execution_safe(target_root: Path, original: OriginalExecution,
                                   report: dict, manifest: bytes, *, supervisor_loss_source_only: bool = False,
                                   official_stop_source_only: bool = False) -> None:
    """Bind terminal readback to the unchanged controller and require its group idle."""
    try:
        saved = json.loads(manifest)
        if (not isinstance(saved, dict) or not isinstance(report, dict)
                or original.file.name != 'execution.json'
                or original.file.parent.parent != target_root / 'runtime/control/executions'
                or controller_handoff._RUN_ID.fullmatch(original.file.parent.name) is None
                or not original.owner_task_id or not original.tool_session_id
                or report.get('kind') != 'preaudit_controller_execution_readback'
                or report.get('read_only') is not True or report.get('children_reaped') is not True
                or report.get('receipt_file') != str(original.file)
                or report.get('owner_task_id') != original.owner_task_id
                or report.get('invocation_id') != original.file.parent.name
                or report.get('tool_session_id') != original.tool_session_id
                or saved.get('kind') != 'preaudit_controller_run_manifest'
                or saved.get('state') not in {'completed', 'failed'}
                or saved.get('children_reaped') is not True
                or any(report.get(key) != saved.get(source) for key, source in (
                    ('run_id', 'run_id'), ('controller_instance_id', 'controller_instance_id'),
                    ('controller_state', 'state'), ('code_revision', 'code_revision')))):
            raise RuntimeError('maintenance_original_execution_untrusted')
        if supervisor_loss_source_only:
            proof = report.get('source_only_proof')
            if (report.get('state') not in {'waiting', 'supervisor_lost'}
                    or report.get('terminal') is not False or report.get('exit_code') is not None
                    or saved['state'] != 'failed' or not isinstance(proof, dict)
                    or proof.get('kind') != 'preaudit_supervisor_loss_source_only'
                    or proof.get('read_only') is not True or proof.get('original_wait_verified') is not False
                    or proof.get('target_root') != str(target_root.resolve())
                    or proof.get('run_id') != saved['run_id']
                    or proof.get('controller_instance_id') != saved['controller_instance_id']
                    or any(proof.get(key) is not True for key in
                        ('readonly_reaped', 'models_reaped', 'supervisor_absent', 'cooldown_clear'))
                    or not isinstance(proof.get('actions'), list)
                    or not isinstance(proof.get('historical_stopped_actions'), list)
                    or proof.get('historical_stop_attribution') != 'not_verified'
                    or not isinstance(proof.get('evidence_files'), dict) or not proof['evidence_files']):
                raise RuntimeError('maintenance_original_execution_untrusted')
        elif (report.get('state') != 'exited' or report.get('terminal') is not True
                or type(report.get('exit_code')) is not int
                or (saved['state'] == 'completed') != (report['exit_code'] == 0)):
            raise RuntimeError('maintenance_original_execution_untrusted')
        if official_stop_source_only:
            proof = report.get('official_stop_proof')
            if (supervisor_loss_source_only or saved['state'] != 'failed' or not isinstance(proof, dict)
                    or proof.get('kind') != 'preaudit_official_stop_source_only'
                    or proof.get('read_only') is not True or proof.get('original_wait_verified') is not True
                    or proof.get('target_root') != str(target_root.resolve())
                    or proof.get('run_id') != saved['run_id']
                    or proof.get('controller_instance_id') != saved['controller_instance_id']
                    or any(proof.get(key) is not True for key in ('readonly_reaped', 'models_reaped', 'supervisor_absent'))
                    or not isinstance(proof.get('cooldown'), dict)
                    or not isinstance(proof.get('interrupted_actions'), list)
                    or not isinstance(proof.get('evidence_files'), dict) or not proof['evidence_files']):
                raise RuntimeError('maintenance_original_execution_untrusted')
            cooldown = proof['cooldown']
            attribution = proof.get('cooldown_attribution', 'approval_run')
            if attribution == 'project_notice':
                notices = proof.get('project_notices')
                if (cooldown.get('run_id') is not None or not isinstance(notices, list) or not notices
                        or any(not isinstance(notice, dict) for notice in notices)
                        or {notice.get('source_sha256') for notice in notices} != set(cooldown.get('sources', []))
                        or len(notices) != len(cooldown.get('sources', []))
                        or cooldown['triggered_at'] < datetime.fromisoformat(saved['completed_at']).timestamp()
                        or not any(notice.get('occurred_at') == cooldown['triggered_at']
                                   and notice.get('notice_code') == cooldown['notice_code'] for notice in notices)):
                    raise RuntimeError('maintenance_original_execution_untrusted')
            elif attribution != 'approval_run' or cooldown.get('run_id') != saved['run_id']:
                raise RuntimeError('maintenance_original_execution_untrusted')
        pid = saved.get('controller_pid')
        if type(pid) is not int or pid < 1:
            raise RuntimeError('maintenance_original_execution_untrusted')
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError('maintenance_original_process_active')
    except (OSError, ValueError) as error:
        raise RuntimeError('maintenance_original_execution_untrusted') from error


def assert_target_environment(target_root: Path) -> None:
    """Reject control path overrides rather than operate on a different runtime."""
    for key in ('OA_FROZEN_WRITER_LOCK_FILE', 'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE',
                'OA_FINANCE_PREAUDIT_STOP_FILE', 'OA_FINANCE_PREAUDIT_RUNTIME_ROOT',
                'OA_FINANCE_PREAUDIT_FLOW_ROOT', 'OA_FINANCE_PREAUDIT_WRITE_POLICY', 'GIT_DIR', 'GIT_WORK_TREE'):
        if os.environ.get(key):
            raise RuntimeError('maintenance_runtime_override')
    if target_root.is_symlink() or not target_root.is_dir():
        raise RuntimeError('maintenance_target_unsafe')


@contextmanager
def target_runtime_leases(target_root: Path) -> Iterator[None]:
    """Exclude controllers and every Writer for the complete maintenance operation."""
    target_root = Path(target_root).absolute()
    assert_target_environment(target_root)
    control = target_root / 'runtime/control'
    writer = control / 'frozen-writer.lock'
    controller = control / 'rolling-controller.lock'
    try:
        with runtime_control.request_writer_lease(runtime_control.GLOBAL_CONTROLLER_KEY,
                                                  controller, blocking=False):
            with runtime_control.request_writer_lease(runtime_control.GLOBAL_WRITER_KEY,
                                                      writer, blocking=False):
                if runtime_control.writer_active(writer) or runtime_control.active_request_writers(writer) != 1:
                    raise RuntimeError('maintenance_writer_active')
                yield
                if runtime_control.writer_active(writer) or runtime_control.active_request_writers(writer) != 1:
                    raise RuntimeError('maintenance_writer_active')
    except BlockingIOError as error:
        raise RuntimeError('maintenance_runtime_active') from error


def assert_live_safe(payload: dict, *, maintenance_leases_held: bool = True,
                     original_closure: bool = False, source_only_proof: dict | None = None,
                     official_stop_proof: dict | None = None,
                     allow_admission_blocked: bool = False) -> None:
    """Require complete live identity/authentication and trusted continuation evidence."""
    if not isinstance(payload, dict) or any(
            not isinstance(payload.get(key), dict)
            for key in ('live_check', 'controller_handoff', 'reconciliation')):
        raise RuntimeError('maintenance_live_evidence_invalid')
    live = payload['live_check']
    if any(not isinstance(live.get(key), dict) for key in ('credentials', 'oa', 'web_session')):
        raise RuntimeError('maintenance_live_evidence_invalid')
    if (live.get('credentials', {}).get('status') != 'ok'
            or live.get('oa', {}).get('authentication') != 'ok'
            or live.get('oa', {}).get('execution_identity') != 'verified'
            or live.get('web_session') != {'status': 'ok', 'authentication': 'ok', 'source': 'rpa_page'}
            or live.get('write_state') != 'not_requested'):
        raise RuntimeError('maintenance_live_identity_untrusted')
    handoff = payload.get('controller_handoff', {})
    if (handoff.get('manifest_state') not in {'completed', 'failed'}
            or handoff.get('children_reaped') is not True
            or handoff.get('restart_mode') != 'fresh_process'
            or handoff.get('restart_verified') is not True):
        raise RuntimeError('maintenance_handoff_untrusted')
    if maintenance_leases_held and (
            handoff.get('controller_active') is not True
            or handoff.get('writer_active') is not False
            or type(handoff.get('active_request_writers')) is not int
            or handoff['active_request_writers'] != 1):
        raise RuntimeError('maintenance_runtime_active')
    if not maintenance_leases_held and (
            handoff.get('controller_active') is not False
            or handoff.get('writer_active') is not False
            or type(handoff.get('active_request_writers')) is not int
            or handoff['active_request_writers'] != 0):
        raise RuntimeError('maintenance_runtime_active')
    reconciled = payload.get('reconciliation', {})
    trusted_continuation = controller_handoff._recovery_reconciliation_is_trusted(reconciled)
    if allow_admission_blocked:
        if original_closure or source_only_proof is not None or official_stop_proof is not None:
            raise RuntimeError('maintenance_reconciliation_untrusted')
        inputs = payload.get('maintenance_current_inputs', {})
        if not isinstance(inputs, dict):
            raise RuntimeError('maintenance_current_inputs_untrusted')
        blocked = inputs.get('admission_blocked_actions', [])
        target = inputs.get('target_root')
        if (not isinstance(blocked, list) or any(not isinstance(path, str) for path in blocked)
                or len(set(blocked)) != len(blocked)):
            raise RuntimeError('maintenance_current_inputs_untrusted')
        if not trusted_continuation and blocked:
            if not isinstance(target, str) or not Path(target).is_absolute():
                raise RuntimeError('maintenance_current_inputs_untrusted')
            findings = reconciled.get('findings', [])
            continuation = reconciled.get('continuation', {})
            quarantine = reconciled.get('quarantine', {})
            if (not isinstance(findings, list) or not isinstance(continuation, dict)
                    or not isinstance(quarantine, dict) or not isinstance(continuation.get('blocking_reasons'), dict)):
                raise RuntimeError('maintenance_reconciliation_untrusted')
            pending = {str(Path(target) / 'runtime/finance_preaudit_flows' / str(row.get('workflow_key'))
                / 'cases' / str(row.get('item_key')) / ('attempt-' + str(row.get('attempt'))) / 'action.json')
                for row in findings if isinstance(row, dict) and row.get('category') == 'pending_not_handled'
                and row.get('local_execution_state') == 'review_required' and row.get('local_write_state') == 'authorized'}
            reasons = continuation.get('blocking_reasons', {})
            quarantined = quarantine.get('pending_quarantined')
            trusted_continuation = (
                set(blocked).issubset(pending)
                and type(continuation.get('isolated_pending')) is int and continuation['isolated_pending'] == 0
                and type(reasons.get('pending_write_state_unresolved')) is int
                and continuation == {'status': 'blocked', 'scope': 'controller', 'isolated_pending': 0,
                    'blocking_reasons': {'pending_write_state_unresolved': len(blocked)}}
                and type(quarantined) is int and quarantined == 0)
    if official_stop_proof is not None:
        inputs = payload.get('maintenance_current_inputs', {})
        if (not isinstance(official_stop_proof, dict) or not isinstance(inputs, dict)
                or not isinstance(official_stop_proof.get('target_root'), str)
                or not Path(official_stop_proof['target_root']).is_absolute()
                or not isinstance(reconciled.get('quarantine'), dict)
                or not isinstance(reconciled.get('continuation'), dict)):
            raise RuntimeError('maintenance_reconciliation_untrusted')
        verified = inputs.get('verified_interrupted_actions')
        uncounted = inputs.get('uncounted_interrupted_actions')
        actions = official_stop_proof.get('interrupted_actions')
        if (official_stop_proof.get('kind') != 'preaudit_official_stop_source_only'
                or not isinstance(actions, list) or any(not isinstance(p, str) for p in actions)
                or not isinstance(verified, list) or any(not isinstance(p, str) for p in verified)
                or not isinstance(uncounted, list) or any(not isinstance(p, str) for p in uncounted)
                or len(set(verified)) != len(verified) or len(set(uncounted)) != len(uncounted)
                or not set(uncounted).issubset(verified) or not set(verified).issubset(actions)
                or not isinstance(reconciled.get('findings'), list)):
            raise RuntimeError('maintenance_reconciliation_untrusted')
        pending_paths = {str(Path(official_stop_proof['target_root']) / 'runtime/finance_preaudit_flows'
            / str(row.get('workflow_key')) / 'cases' / str(row.get('item_key')) / ('attempt-' + str(row.get('attempt')))
            / 'action.json') for row in reconciled['findings'] if isinstance(row, dict)
            and row.get('category') == 'pending_not_handled'}
        if set(verified) != set(actions).intersection(pending_paths):
            raise RuntimeError('maintenance_reconciliation_untrusted')
        if not trusted_continuation:
            continuation = reconciled.get('continuation', {})
            quarantined = reconciled.get('quarantine', {}).get('pending_quarantined')
            isolated = continuation.get('isolated_pending')
            trusted_continuation = (
                continuation == {'status': 'ready', 'scope': 'item', 'isolated_pending': isolated, 'blocking_reasons': {}}
                and type(isolated) is int and type(quarantined) is int and quarantined >= 0
                and len(uncounted) > 0 and isolated == quarantined + len(uncounted))
    withdrawn_action = None
    if source_only_proof is not None and not isinstance(source_only_proof, dict):
        raise RuntimeError('maintenance_current_inputs_untrusted')
    if source_only_proof is not None and source_only_proof.get('kind') == 'preaudit_material_failure_source_only':
        inputs = payload.get('maintenance_current_inputs', {})
        actions = source_only_proof.get('actions')
        continuation = reconciled.get('continuation', {})
        isolated = continuation.get('isolated_pending')
        if (set(source_only_proof) != {'kind', 'target_root', 'actions'}
                or not isinstance(actions, list) or not actions
                or any(not isinstance(action, str) for action in actions)
                or len(set(actions)) != len(actions)
                or not isinstance(inputs, dict)
                or inputs.get('target_root') != source_only_proof['target_root']
                or inputs.get('verified_material_failure_actions') != actions
                or inputs.get('material_failure_recovery_ready') is not True
                or type(isolated) is not int or isolated < 0
                or continuation != {'status': 'blocked', 'scope': 'controller', 'isolated_pending': isolated,
                    'blocking_reasons': {'pending_failure_not_isolated': len(actions)}}
                or type(reconciled.get('quarantine', {}).get('pending_quarantined')) is not int
                or reconciled['quarantine']['pending_quarantined'] != isolated + len(actions)):
            raise RuntimeError('maintenance_reconciliation_untrusted')
        trusted_continuation = True
    elif source_only_proof is not None and source_only_proof.get('kind') == 'preaudit_confirmed_withdrawal_source_only':
        inputs = payload.get('maintenance_current_inputs', {})
        withdrawn_action = source_only_proof.get('withdrawn_action')
        if (source_only_proof.get('withdrawal_confirmed') is not True or not isinstance(withdrawn_action, str)
                or not Path(withdrawn_action).is_absolute()
                or inputs.get('verified_withdrawn_actions') != [withdrawn_action]
                or reconciled.get('continuation') != {'status':'blocked', 'scope':'controller',
                    'isolated_pending':0, 'blocking_reasons':{'pending_write_state_unresolved':1}}
                or reconciled.get('quarantine', {}).get('pending_quarantined') != 0):
            raise RuntimeError('maintenance_reconciliation_untrusted')
        trusted_continuation = True
    elif source_only_proof is not None:
        current_inputs = payload.get('maintenance_current_inputs')
        if not isinstance(current_inputs, dict) or not isinstance(source_only_proof, dict):
            raise RuntimeError('maintenance_current_inputs_untrusted')
        preserved = current_inputs.get('preserved_stop_failures')
        continuation = reconciled.get('continuation', {})
        quarantine = reconciled.get('quarantine', {})
        if (not isinstance(continuation, dict) or not isinstance(quarantine, dict)
                or not isinstance(preserved, list) or any(not isinstance(path, str) for path in preserved)
                or len(set(preserved)) != len(preserved)
                or not set(preserved).issubset(source_only_proof.get('historical_stopped_actions', []))):
            raise RuntimeError('maintenance_reconciliation_untrusted')
        quarantined = quarantine.get('pending_quarantined')
        if preserved:
            trusted_continuation = (
                continuation == {'status': 'blocked', 'scope': 'controller',
                    'isolated_pending': continuation.get('isolated_pending'),
                    'blocking_reasons': {'pending_failure_not_isolated': len(preserved)}}
                and type(continuation.get('isolated_pending')) is int
                and continuation['isolated_pending'] >= 0
                and type(quarantined) is int
                and quarantined == continuation['isolated_pending'] + len(preserved))
    if (reconciled.get('kind') != 'preaudit_reconciliation'
            or reconciled.get('read_only') is not True
            or not trusted_continuation
            or not isinstance(reconciled.get('findings'), list)):
        raise RuntimeError('maintenance_reconciliation_untrusted')
    for finding in reconciled['findings']:
        if not isinstance(finding, dict):
            raise RuntimeError('maintenance_reconciliation_untrusted')
        withdrawn = (withdrawn_action is not None and finding.get('category') == 'pending_not_handled'
            and finding.get('local_execution_state') == 'closed' and finding.get('local_write_state') == 'submitted'
            and str(Path(source_only_proof['target_root']) / 'runtime/finance_preaudit_flows'
                / str(finding.get('workflow_key')) / 'cases' / str(finding.get('item_key'))
                / ('attempt-' + str(finding.get('attempt'))) / 'action.json') == withdrawn_action)
        if (finding.get('category') == 'pending_handled_conflict'
                or (finding.get('category') == 'identity_conflict'
                    and finding.get('recommendation') != 'deferred_historical_cleanup')
                or (finding.get('category') == 'pending_not_handled'
                    and finding.get('local_write_state') in {'unknown', 'submitting', 'submitted'} and not withdrawn)):
            raise RuntimeError('maintenance_current_write_unresolved')
    if not original_closure:
        from .production_config import FINANCE_WORKFLOWS
        proof = payload.get('maintenance_current_inputs')
        receipt = payload.get('maintenance_status_file')
        if (not isinstance(proof, dict) or not isinstance(receipt, dict)
                or proof.get('kind') != 'preaudit_maintenance_current_inputs'
                or proof.get('status') != 'verified' or proof.get('read_only') is not True
                or proof.get('write_state') != 'not_requested'
                or not isinstance(receipt.get('sha256'), str) or len(receipt['sha256']) != 64
                or any(character not in '0123456789abcdef' for character in receipt['sha256'])
                or not isinstance(proof.get('policy_sha256'), str) or len(proof['policy_sha256']) != 64
                or any(character not in '0123456789abcdef' for character in proof['policy_sha256'])
                or proof.get('status_sha256') != receipt.get('sha256')
                or not isinstance(proof.get('authorized_workflows'), list)
                or any(not isinstance(value, str) for value in proof['authorized_workflows'])
                or sorted(proof.get('authorized_workflows') or []) != sorted((*FINANCE_WORKFLOWS, 'packaging_layout_approval'))
                or type(proof.get('pending_frozen_count')) is not int or proof['pending_frozen_count'] < 0
                or type(proof.get('validated_frozen_count')) is not int
                or proof['validated_frozen_count'] != proof['pending_frozen_count']):
            raise RuntimeError('maintenance_current_inputs_untrusted')
        if proof['pending_frozen_count'] != sum(row.get('category') == 'pending_not_handled' for row in reconciled['findings']):
            raise RuntimeError('maintenance_current_inputs_untrusted')


# This fixed bridge imports only validators already installed in the target tree.
# It never calls an OA port, renews a grant, persists state, or imports candidate code.
_CURRENT_INPUTS_READER = _HISTORICAL_STOP_VALIDATOR + r'''
import hashlib, json, os, stat, sys
from pathlib import Path
from apps.oa_approval import auth
from apps.oa_finance_preaudit import flow_engine, production_write, reconciliation, item_failure, recovery_receipt
root = Path.cwd().resolve()
path, expected_hash, workflows_json, source_only_json, admission_json = sys.argv[1:]
required = set(json.loads(workflows_json))
source_only = json.loads(source_only_json)
allow_admission_blocked = json.loads(admission_json)
code = 'maintenance_current_inputs_untrusted'

def failure_files(attempt):
    result = {}
    for file in sorted(attempt.rglob('*')):
        if file.is_symlink():
            raise ValueError()
        if file.is_file():
            info = file.stat()
            result[str(file.relative_to(attempt))] = [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_uid,
                info.st_size, hashlib.sha256(file.read_bytes()).hexdigest()]
    return result

def validate_collecting(flows, attempt, state):
    reader = flow_engine.FlowEngine(root=flows, discover_candidates=None, collect_material=None)
    if (not reader._attempt_identity_matches(attempt, state, require_material=False)
            or state.get('write_state') != 'not_requested' or state.get('write_authorized') is not False
            or state.get('mode') not in {'preview', 'controlled_write'} or state.get('action') != 'collecting'
            or any(os.path.lexists(attempt / name) for name in ('writer-boundary.json', 'execution-receipt.json',
                'review-decision.json', 'remark.txt', 'submission-envelope.json'))):
        raise ValueError()
    claim_state = dict(state)
    if (state.get('recovered_from_attempt') is not None or state.get('recovery_receipt_fingerprint') is not None
            or os.path.lexists(attempt / recovery_receipt.NAME)):
        if recovery_receipt.projection(attempt, state).get('receipt_status') != 'verified':
            raise ValueError()
        # begin() binds only state to the source-attempt receipt after writing claim.
        claim_state.pop('recovery_receipt_fingerprint', None)
    action = flow_engine._read_object(attempt / 'action.json', code='persistent_state_invalid')
    fields = ('material_file', 'review_packet_file', 'material_fingerprint', 'evidence_fingerprint')
    frozen = {key: state.get(key) for key in fields}
    has_frozen = any(value is not None for value in frozen.values())
    if action.get('execution_state') == 'collecting':
        # Preparation enriches only state with the four frozen fields; the original
        # claim, including any recovery provenance, remains unchanged in action.
        if claim_state != action and (not has_frozen or claim_state != {**action, **frozen}):
            raise ValueError()
    elif action.get('execution_state') == 'review_required':
        # action is written immediately before state. The saved collecting state
        # remains unwritten; validate this exact public action contract, not a grant.
        mode = action.get('mode')
        fingerprint = action.get('authorization_fingerprint')
        token = action.get('action_token')
        if (not has_frozen or mode not in {'preview', 'controlled_write'}
                or not isinstance(fingerprint, str) or not fingerprint.startswith('sha256:')
                or not isinstance(token, str) or not token):
            raise ValueError()
        expected = flow_engine._action_payload(action='review_required', execution_state='review_required',
            business_state='not_available', write_state='authorized' if mode == 'controlled_write' else 'not_requested',
            workflow_key=state['workflow_key'], mode=mode, write_authorized=mode == 'controlled_write',
            authorization_fingerprint=fingerprint, action_token=token, action_file=str(attempt / 'action.json'),
            state_file=str(attempt / 'state.json'), review_packet_file=str(attempt / 'review-packet.json'),
            events_file=str(attempt / 'observability.json'), case_key=state['case_key'], attempt=state['attempt'],
            **({'rule_version':flow_engine._FLOW_SPECS[state['workflow_key']]['rule_version']}
                if state['workflow_key'] in flow_engine._EXPLICIT_RULE_VERSION_WORKFLOWS else {}),
            **{key:state[key] for key in ('recovered_from_attempt', 'recovery_reason', 'recovery_no_write_evidence') if key in state})
        if action != expected:
            raise ValueError()
    else:
        raise ValueError()
    validation_state = state
    if state['workflow_key'] in flow_engine._EXPLICIT_RULE_VERSION_WORKFLOWS:
        version = flow_engine._FLOW_SPECS[state['workflow_key']]['rule_version']
        if state.get('rule_version') not in (None, version):
            raise ValueError()
        # Preparation emits the current packet before persisting rule_version in
        # review_required state. Validate that current contract without writing it.
        validation_state = {**state, 'rule_version':version}
    if has_frozen:
        flow_engine.validate_frozen_review_inputs(attempt_root=attempt, state=validation_state, workflow_key=state['workflow_key'])
    else:
        # Bundle and packet can be complete before their paths/hashes reach state.
        # Verify these files using installed validators without persisting a projection.
        material = attempt / 'material.json'
        packet = attempt / 'review-packet.json'
        if os.path.lexists(material):
            if not flow_engine._managed_existing_path(material, flows):
                raise ValueError()
            verified = flow_engine._validate_material_bundle(material, expected_request_id=state['request_id'],
                attachment_root=attempt / 'attachments', prepared_root=attempt / 'prepared')
            contents = flow_engine._read_object(material, code='frozen_material_changed')
            if contents.get('workflow_key') != state['workflow_key']:
                raise ValueError()
            flow_engine._validate_flow_material(state['workflow_key'], contents)
            if os.path.lexists(packet):
                projection = {**validation_state, 'material_file':str(material), 'review_packet_file':str(packet),
                    'material_fingerprint':verified['material_fingerprint'],
                    'evidence_fingerprint':flow_engine._evidence_fingerprint(contents)}
                flow_engine.validate_frozen_review_inputs(attempt_root=attempt, state=projection, workflow_key=state['workflow_key'])
        elif os.path.lexists(packet):
            raise ValueError()

try:
    if any(not Path(module.__file__).resolve().is_relative_to(root) for module in (flow_engine, production_write, reconciliation, item_failure, recovery_receipt)):
        raise ValueError()
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError()
    snapshot = json.loads(raw)
    policy = root / 'runtime/control/write-policy.json'
    code = 'maintenance_authorization_untrusted'
    auth.load_local_env()
    configured_policy = os.environ.get('OA_FINANCE_PREAUDIT_WRITE_POLICY')
    if configured_policy and Path(configured_policy).expanduser().absolute() != policy:
        raise ValueError()
    metadata = policy.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise ValueError()
    if policy.is_symlink() or not required.issubset(production_write._authorized_workflows(policy)):
        raise ValueError()
    policy_bytes = policy.read_bytes()
    policy_hash = hashlib.sha256(policy_bytes).hexdigest()
    code = 'maintenance_frozen_inputs_untrusted'
    flows = root / 'runtime/finance_preaudit_flows'
    states = reconciliation.latest_states(flows)
    by_key = {(s['workflow_key'], s['case_key'], s['attempt']): s for s in states}
    pending = [item for item in snapshot['reconciliation']['findings'] if item['category'] == 'pending_not_handled']
    validated = 0
    preserved = []
    interrupted = []
    uncounted = []
    withdrawn = []
    admission_blocked = []
    material_failures = []
    failure_fingerprints = {}
    material_source_only = source_only is not None and source_only.get('kind') == 'preaudit_material_failure_source_only'
    if material_source_only:
        if (set(source_only) != {'kind', 'target_root', 'actions'} or source_only['target_root'] != str(root)
                or not isinstance(source_only['actions'], list) or not source_only['actions']
                or any(not isinstance(action, str) for action in source_only['actions'])
                or len(set(source_only['actions'])) != len(source_only['actions'])):
            raise ValueError()
    for item in pending:
        state = by_key[(item['workflow_key'], item['item_key'], item['attempt'])]
        attempt = flows / state['workflow_key'] / 'cases' / state['case_key'] / ('attempt-' + str(state['attempt']))
        if state['execution_state'] == 'collecting':
            validate_collecting(flows, attempt, state)
        elif state['execution_state'] == 'review_required':
            if allow_admission_blocked:
                if source_only is not None:
                    raise ValueError()
                reader = flow_engine.FlowEngine(root=flows, discover_candidates=None, collect_material=None)
                _, persisted, _ = reader._open_attempt(attempt / 'action.json', require_reviewable=False)
                if persisted != state:
                    raise ValueError()
                material = flow_engine._read_object(attempt / 'material.json', code='frozen_material_changed')
                try:
                    flow_engine._require_reviewable_material(state['workflow_key'], material)
                except ValueError as error:
                    if (str(error) != 'material_collection_failed'
                            or any(os.path.lexists(attempt / name) for name in ('writer-boundary.json',
                                'execution-receipt.json', 'review-decision.json', 'remark.txt', 'submission-envelope.json'))):
                        raise
                    admission_blocked.append(str(attempt / 'action.json'))
            elif not flow_engine.FlowEngine.resumable_review(flows, state):
                raise ValueError()
        elif (state['execution_state'] == 'closed' and source_only is not None
                and source_only.get('kind') == 'preaudit_confirmed_withdrawal_source_only'):
            if (state['workflow_key'] not in flow_engine._FINANCE_WORKFLOW_KEYS
                    or set(source_only) != {'kind','target_root','withdrawn_action','source_fingerprints','withdrawal_confirmed'}
                    or source_only['target_root'] != str(root) or source_only['withdrawal_confirmed'] is not True
                    or source_only['withdrawn_action'] != str(attempt / 'action.json')
                    or source_only['source_fingerprints'] != recovery_receipt._fingerprints(attempt)
                    or not flow_engine.FlowEngine.confirmed_submission(flows, state)):
                raise ValueError()
            withdrawn.append(str(attempt / 'action.json'))
        elif state['execution_state'] == 'needs_admin':
            isolated = item_failure.isolated(attempt, state)
            stopped_collection = False
            selected_failure = material_source_only and str(attempt / 'action.json') in source_only['actions']
            if selected_failure:
                receipt = item_failure.verified_receipt(attempt, state)
                if (receipt is None or receipt.get('scope') != 'controller'
                        or not item_failure.material_failure_without_write(attempt, state)
                        or not flow_engine.FlowEngine.recoverable_no_write(flows, attempt / 'action.json')):
                    raise ValueError()
                material_failures.append(str(attempt / 'action.json'))
                failure_fingerprints[str(attempt / 'action.json')] = failure_files(attempt)
            if source_only is not None and str(attempt / 'action.json') in source_only.get('historical_stopped_actions', []):
                stopped_collection = historical_stopped_collection(attempt, state)
            if not (isolated or reconciliation._trusted_quarantine(flows, state) or stopped_collection or selected_failure):
                raise ValueError()
            if stopped_collection:
                preserved.append(str(attempt / 'action.json'))
            if (source_only is not None and source_only.get('kind') == 'preaudit_official_stop_source_only'
                    and state.get('reason') == 'codex_review_interrupted'):
                action = str(attempt / 'action.json')
                if not isolated or action not in source_only['interrupted_actions']:
                    raise ValueError()
                interrupted.append(action)
                if not reconciliation._trusted_quarantine(flows, state):
                    uncounted.append(action)
            # A verified item receipt binds the failure artifacts and proves no write;
            # a material failure cannot also promise a valid frozen material bundle.
            material_failure = (isolated or selected_failure) and state.get('reason') == 'material_collection_failed'
            if not material_failure and (state.get('material_file') is not None or state.get('review_packet_file') is not None):
                flow_engine.validate_frozen_review_inputs(attempt_root=attempt, state=state, workflow_key=state['workflow_key'])
        else:
            raise ValueError()
        validated += 1
    if (source_only is not None and source_only.get('kind') == 'preaudit_confirmed_withdrawal_source_only'
            and withdrawn != [source_only['withdrawn_action']]):
        raise ValueError()
    if material_source_only:
        from scripts import finance_preaudit_concurrency_probe as controller
        if (set(material_failures) != set(source_only['actions'])
                or not controller._continuation_is_ready(snapshot['reconciliation'],
                    recovery_action_files=[Path(action) for action in source_only['actions']])
                or any(failure_files(Path(action).parent) != fingerprint
                    for action, fingerprint in failure_fingerprints.items())):
            raise ValueError()
    current_policy = policy.lstat()
    if ((metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_mode)
            != (current_policy.st_dev, current_policy.st_ino, current_policy.st_mtime_ns, current_policy.st_mode)
            or policy.read_bytes() != policy_bytes or Path(path).read_bytes() != raw or reconciliation.latest_states(flows) != states):
        raise ValueError()
    print(json.dumps({'kind':'preaudit_maintenance_current_inputs','status':'verified','read_only':True,
        'write_state':'not_requested','target_root':str(root),'status_sha256':expected_hash,
        'policy_sha256':policy_hash,'authorized_workflows':sorted(required),
        'pending_frozen_count':len(pending),'validated_frozen_count':validated,
        **({'admission_blocked_actions':admission_blocked} if allow_admission_blocked else {}),
        **({'verified_material_failure_actions':source_only['actions'], 'material_failure_recovery_ready':True,
            'material_failure_fingerprints':failure_fingerprints} if material_source_only else {}),
        **({'verified_withdrawn_actions':withdrawn}
            if source_only is not None and source_only.get('kind') == 'preaudit_confirmed_withdrawal_source_only'
            else {'verified_interrupted_actions':sorted(interrupted),'uncounted_interrupted_actions':sorted(uncounted)}
            if source_only is not None and source_only.get('kind') == 'preaudit_official_stop_source_only'
            else {'preserved_stop_failures':sorted(preserved)} if source_only is not None else {})}))
except (OSError, ValueError, RuntimeError, KeyError, TypeError):
    print(json.dumps({'status':'blocked','error_code':code}))
    raise SystemExit(1)
'''


def read_current_inputs(target_root: Path, receipt: dict, *, require_reaped: bool = False,
                        source_only_proof: dict | None = None, official_stop_proof: dict | None = None,
                        allow_admission_blocked: bool = False) -> dict:
    """Obtain current policy/frozen proof from the target's existing validators."""
    from .production_config import FINANCE_WORKFLOWS
    target_root = Path(target_root).expanduser().absolute()
    assert_target_environment(target_root)
    if (target_root / '.venv').is_symlink() or not (target_root / '.venv/bin/python').is_file():
        raise RuntimeError('maintenance_interpreter_untrusted')
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    try:
        result = _run_readonly([str(target_root / '.venv/bin/python'), '-B', '-E', '-c', _CURRENT_INPUTS_READER,
            receipt['path'], receipt['sha256'], json.dumps([*FINANCE_WORKFLOWS, 'packaging_layout_approval']),
            json.dumps(official_stop_proof if official_stop_proof is not None else source_only_proof),
            json.dumps(allow_admission_blocked)],
            target_root=target_root, environment=environment, timeout=300, require_reaped=require_reaped)
        if len(result.stdout.encode()) > 1_000_000:
            raise RuntimeError('maintenance_current_inputs_untrusted')
        proof = json.loads(result.stdout)
        if result.returncode:
            code = proof.get('error_code') if isinstance(proof, dict) else None
            raise RuntimeError(code if code in {'maintenance_authorization_untrusted', 'maintenance_frozen_inputs_untrusted'}
                               else 'maintenance_current_inputs_untrusted')
        if not isinstance(proof, dict) or proof.get('target_root') != str(target_root.resolve()):
            raise RuntimeError('maintenance_current_inputs_untrusted')
        return proof
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise RuntimeError('maintenance_current_inputs_untrusted') from error


def read_live_status(target_root: Path, *, require_reaped: bool = False,
                     source_only_proof: dict | None = None, official_stop_proof: dict | None = None,
                     allow_admission_blocked: bool = False) -> dict:
    """Await target Python's own readonly status and verify its file-backed receipt."""
    target_root = Path(target_root).absolute()
    assert_target_environment(target_root)
    interpreter = target_root / '.venv/bin/python'
    if (target_root / '.venv').is_symlink() or not interpreter.is_file():
        raise RuntimeError('maintenance_interpreter_untrusted')
    # The target interpreter imports only target source. Never inject candidate source
    # into the production interpreter or reuse a previously captured JSON snapshot.
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    evidence_dir = Path(tempfile.mkdtemp(prefix='oa-maintenance-status-')).resolve()
    evidence_file = evidence_dir / 'status.json'
    try:
        result = _run_readonly(
            [str(interpreter), '-B', '-E', '-m', 'apps.oa_finance_preaudit.cli',
             'status', '--check-live', '--reconcile', '--summary',
             '--output-file', str(evidence_file)],
            target_root=target_root, environment=environment, timeout=300, require_reaped=require_reaped,
        )
        if result.returncode != 0:
            raise RuntimeError('maintenance_live_status_failed')
        summary = json.loads(result.stdout)
        if not isinstance(summary, dict) or not isinstance(summary.get('status_file'), dict):
            raise RuntimeError('maintenance_live_receipt_invalid')
        receipt = summary['status_file']
        if receipt.get('path') != str(evidence_file):
            raise RuntimeError('maintenance_live_receipt_invalid')
        raw = runtime_control.read_control_bytes(evidence_file, max_bytes=64 * 1024 * 1024)
        if (raw is None or type(receipt.get('bytes')) is not int
                or receipt['bytes'] != len(raw)
                or receipt.get('sha256') != hashlib.sha256(raw).hexdigest()):
            raise RuntimeError('maintenance_live_receipt_invalid')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError('maintenance_live_receipt_invalid')
        payload['maintenance_status_file'] = receipt
        payload['maintenance_current_inputs'] = read_current_inputs(target_root, receipt,
            require_reaped=require_reaped, source_only_proof=source_only_proof, official_stop_proof=official_stop_proof,
            allow_admission_blocked=allow_admission_blocked)
        return payload
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RuntimeError('maintenance_live_status_failed') from error
