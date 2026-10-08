"""Target-local leases and verified read-only evidence for production maintenance."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Iterator

from apps.oa_approval import runtime_control
from . import controller_handoff


def assert_target_environment(target_root: Path) -> None:
    """Reject control path overrides rather than operate on a different runtime."""
    for key in ('OA_FROZEN_WRITER_LOCK_FILE', 'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE',
                'OA_FINANCE_PREAUDIT_STOP_FILE', 'OA_FINANCE_PREAUDIT_RUNTIME_ROOT',
                'OA_FINANCE_PREAUDIT_FLOW_ROOT', 'GIT_DIR', 'GIT_WORK_TREE'):
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


def assert_live_safe(payload: dict, *, maintenance_leases_held: bool = True) -> None:
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
    if (reconciled.get('kind') != 'preaudit_reconciliation'
            or reconciled.get('read_only') is not True
            or not controller_handoff._recovery_reconciliation_is_trusted(reconciled)
            or not isinstance(reconciled.get('findings'), list)):
        raise RuntimeError('maintenance_reconciliation_untrusted')
    # Continuation verifies isolation/frozen state and authorization contracts; retain
    # a separate explicit current-write gate even if a caller supplies malformed counts.
    for finding in reconciled['findings']:
        if not isinstance(finding, dict):
            raise RuntimeError('maintenance_reconciliation_untrusted')
        if (finding.get('category') == 'pending_handled_conflict'
                or (finding.get('category') == 'identity_conflict'
                    and finding.get('recommendation') != 'deferred_historical_cleanup')
                or (finding.get('category') == 'pending_not_handled'
                    and finding.get('local_write_state') in {'unknown', 'submitting', 'submitted'})):
            raise RuntimeError('maintenance_current_write_unresolved')


def read_live_status(target_root: Path) -> dict:
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
        result = subprocess.run(
            [str(interpreter), '-B', '-E', '-m', 'apps.oa_finance_preaudit.cli',
             'status', '--check-live', '--reconcile', '--summary',
             '--output-file', str(evidence_file)],
            cwd=target_root, env=environment, capture_output=True, text=True,
            check=False, timeout=300,
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
        return payload
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RuntimeError('maintenance_live_status_failed') from error
