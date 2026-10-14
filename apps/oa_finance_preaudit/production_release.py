"""One fail-closed gate for readonly release preflight and authorized source sync."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import tomllib
from typing import Callable

from apps.oa_approval import runtime_control
from . import maintenance_runtime

ORIGIN = 'https://github.com/CleverC2200/oa-workspace-minimal.git'
_REVISION = re.compile(r'^[0-9a-f]{40}$')


@dataclass(frozen=True)
class ProductionAutomation:
    file: Path
    project_id: str
    schedule: str

    def findings(self, production_root: Path) -> list[str]:
        from . import production_config
        raw = runtime_control.read_control_bytes(self.file)
        if raw is None:
            raise RuntimeError('production_release_automation_missing')
        configuration = tomllib.loads(raw.decode('utf-8'))
        return production_config.automation_findings(configuration,
            production_project_id=self.project_id, expected_schedule=self.schedule, production_root=production_root)


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null', *args], cwd=root,
                                env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'),
                                capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError('production_release_git_failed') from error
    if result.returncode:
        raise RuntimeError('production_release_git_failed')
    return result.stdout.strip()


def _source(root: Path, expected_revision: str, expected_origin: str, *, synchronized: bool = False) -> str:
    if (root.name != 'oa-workspace-minimal' or root.is_symlink()
            or (root / '.git').is_symlink() or not (root / '.git').is_dir()
            or Path(_git(root, 'rev-parse', '--show-toplevel')).resolve() != root.resolve()
            or _git(root, 'rev-parse', '--git-common-dir') != '.git'):
        raise RuntimeError('production_release_clone_required')
    if _git(root, 'branch', '--show-current') != 'main':
        raise RuntimeError('production_release_main_required')
    entries = _git(root, 'worktree', 'list', '--porcelain').split('\n\n')
    if len(entries) != 1 or 'branch refs/heads/main' not in entries[0].splitlines():
        raise RuntimeError('production_release_unique_worktree_required')
    if _git(root, 'status', '--porcelain', '--untracked-files=normal'):
        raise RuntimeError('production_release_dirty')
    if _git(root, 'remote', 'get-url', 'origin') != expected_origin:
        raise RuntimeError('production_release_origin_mismatch')
    remote = _git(root, 'ls-remote', '--exit-code', 'origin', 'refs/heads/main').split()
    if (len(remote) != 2 or remote != [expected_revision, 'refs/heads/main']
            or _git(root, 'rev-parse', 'origin/main') != expected_revision):
        raise RuntimeError('production_release_revision_mismatch')
    head = _git(root, 'rev-parse', 'HEAD')
    try:
        _git(root, 'merge-base', '--is-ancestor', head, expected_revision)
    except RuntimeError as error:
        raise RuntimeError('production_release_not_fast_forward') from error
    if synchronized and head != expected_revision:
        raise RuntimeError('production_release_revision_mismatch')
    return head


def _assert_no_stop(root: Path) -> None:
    raw = os.environ.get('OA_FINANCE_PREAUDIT_STOP_REQUESTED', '').strip().lower()
    marker = root / 'runtime/stop-preaudit.flag'
    if raw in {'1', 'true', 'yes', 'y', 'on'} or marker.exists() or marker.is_symlink():
        raise RuntimeError('production_release_stop_requested')


def _pinned_control(path: Path, *, private: bool = True,
                    max_bytes: int = 1_048_576) -> tuple[tuple[int, ...], bytes]:
    pinned = path.lstat()
    if (not stat.S_ISREG(pinned.st_mode) or pinned.st_uid != os.getuid()
            or pinned.st_mode & (0o077 if private else 0o022)):
        raise RuntimeError('production_release_control_untrusted')
    raw = runtime_control.read_control_bytes(path, max_bytes=max_bytes)
    current = path.lstat()
    fields = ('st_dev', 'st_ino', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_uid', 'st_size')
    identity = tuple(getattr(pinned, name) for name in fields)
    if raw is None or identity != tuple(getattr(current, name) for name in fields):
        raise RuntimeError('production_release_control_untrusted')
    return identity, raw


def _stopped_controls(root: Path, automation: ProductionAutomation, *, official_stop_source_only: bool = False,
                      user_stop_source_only: bool = False) -> tuple:
    if os.environ.get('OA_FINANCE_PREAUDIT_STOP_REQUESTED', '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}:
        raise RuntimeError('production_release_stop_requested')
    stop = _pinned_control(root / 'runtime/stop-preaudit.flag')
    if official_stop_source_only and user_stop_source_only:
        raise RuntimeError('production_release_source_only_modes_conflict')
    expected_stop = (b'stop_requested\n' if user_stop_source_only else
                     b'stop_requested:codex_official_error_cooldown\n' if official_stop_source_only
                     else b'stop_requested:codex_review_technical_failure\n')
    if stop[1] != expected_stop:
        raise RuntimeError('production_release_stop_not_repairable')
    native_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().absolute()
    registered = native_home / 'automations/oa-skill/automation.toml'
    if automation.file.expanduser().absolute() != registered:
        raise RuntimeError('production_release_automation_identity_mismatch')
    cron = _pinned_control(automation.file, private=False)
    configuration = tomllib.loads(cron[1].decode('utf-8'))
    if configuration.get('id') != 'oa-skill':
        raise RuntimeError('production_release_automation_identity_mismatch')
    if configuration.get('status') != 'PAUSED':
        raise RuntimeError('production_release_automation_not_paused')
    findings = automation.findings(root)
    if findings:
        raise RuntimeError('production_release_configuration_invalid:' + ','.join(findings))
    return (stop, cron, _pinned_control(root / 'runtime/control/approval-guard/cooldown.json')) if official_stop_source_only else (stop, cron)


def _control_proof(pinned: tuple) -> dict:
    identity, raw = pinned
    return dict(zip(('device', 'inode', 'mtime_ns', 'ctime_ns', 'mode', 'uid', 'bytes'), identity),
                sha256=hashlib.sha256(raw).hexdigest())


def _stopped_observation(root: Path, automation: ProductionAutomation | None, *, official_stop_source_only: bool = False) -> dict:
    observed = {}
    files = [('stop', root / 'runtime/stop-preaudit.flag'), ('automation', automation.file if automation else None)]
    if official_stop_source_only:
        files.append(('cooldown', root / 'runtime/control/approval-guard/cooldown.json'))
    for key, path in files:
        try:
            if path is None:
                raise RuntimeError('production_release_automation_required')
            pinned = _pinned_control(path, private=key != 'automation')
            observed[key] = _control_proof(pinned)
            if key == 'automation':
                state = tomllib.loads(pinned[1].decode('utf-8')).get('status')
                observed['automation_state'] = state if state in {'PAUSED', 'ACTIVE'} else 'unknown'
        except (OSError, ValueError, RuntimeError):
            observed[key] = {'status': 'untrusted'}
            if key == 'automation':
                observed['automation_state'] = 'unknown'
    return observed


def _stopped_receipt(root: Path, automation: ProductionAutomation | None, before: dict | None,
                     *, official_stop_source_only: bool = False) -> dict:
    after = _stopped_observation(root, automation, official_stop_source_only=official_stop_source_only)
    return {'stopped_maintenance': True, 'stopped_controls_before': before, 'stopped_controls_after': after,
            'stop_preserved': bool(before and 'sha256' in before['stop'] and before['stop'] == after['stop']),
            'automation_preserved': bool(before and 'sha256' in before['automation'] and before['automation'] == after['automation']),
            'automation_state': after['automation_state'],
            **({'cooldown_preserved': bool(before and 'sha256' in before.get('cooldown', {})
                and before['cooldown'] == after['cooldown'])} if official_stop_source_only else {})}


def candidate_configuration_findings(root: Path, revision: str) -> list[str]:
    """Inspect candidate tracked text without importing or executing candidate code."""
    from . import production_config
    with tempfile.TemporaryDirectory(prefix='oa-release-source-') as directory:
        candidate = Path(directory)
        for name in production_config.REPOSITORY_INPUTS:
            target = candidate / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_git(root, 'show', f'{revision}:{name}') + '\n', encoding='utf-8')
        return production_config.repository_findings(candidate)


def _observed_revision(root: Path) -> str | None:
    try:
        revision = _git(root, 'rev-parse', 'HEAD')
    except RuntimeError:
        return None
    return revision if _REVISION.fullmatch(revision) else None


def _failed_release(root: Path, before: str | None, expected: str, requested: bool, error: Exception,
                    *, stopped_maintenance: bool = False, automation: ProductionAutomation | None = None,
                    controls_before: dict | None = None, supervisor_loss_source_only: bool = False,
                    official_stop_source_only: bool = False, user_stop_source_only: bool = False) -> dict:
    after = _observed_revision(root)
    state = 'unknown' if before is None or after is None else ('not_updated' if after == before else 'updated')
    return {'schema_version': 1, 'kind': 'production_source_release', 'status': 'blocked',
            'code': str(error), 'write_state': 'not_requested', 'before_revision': before,
            'after_revision': after, 'revision': after, 'expected_revision': expected,
            'requested_revision': expected, 'source_sync_requested': requested,
            'source_sync_state': state, 'next_process_ready': False,
            'production_acceptance': 'not_verified',
            **({'supervisor_loss_source_only': True, 'original_wait_verified': False,
                'approval_recovery_allowed': False, 'original_evidence_preserved': 'unknown'}
                if supervisor_loss_source_only else {}),
            **({'official_stop_source_only': True, 'original_wait_verified': 'unknown',
                'approval_recovery_allowed': False, 'original_evidence_preserved': 'unknown'}
                if official_stop_source_only else {}),
            **({'user_stop_source_only': True, 'original_wait_verified': 'unknown',
                'approval_recovery_allowed': False, 'original_evidence_preserved': 'unknown'}
                if user_stop_source_only else {}),
            **(_stopped_receipt(root, automation, controls_before,
                official_stop_source_only=official_stop_source_only) if stopped_maintenance else {})}


def release_source(
    target_root: Path, *, expected_revision: str, confirmed_sync: bool = False,
    expected_origin: str = ORIGIN, automation: ProductionAutomation | None = None,
    check_live: Callable[[Path], dict] | None = None,
    check_configuration: Callable[[Path, str], list[str]] | None = None,
    stopped_maintenance: bool = False,
    supervisor_loss_source_only: bool = False,
    official_stop_source_only: bool = False,
    user_stop_source_only: bool = False,
    original_execution: maintenance_runtime.OriginalExecution | None = None,
    check_original: Callable[[Path, maintenance_runtime.OriginalExecution], dict] | None = None,
) -> dict:
    """Preflight, optionally ff-only sync, and verify while retaining both leases.

    The default adapter reads target-local production state freshly. Injected
    adapters are for offline callers/tests; the CLI never accepts cached JSON.
    No OA write, stop mutation, automation change, fetch, dependency installation,
    or forced reset is performed. Fetch origin explicitly before calling.
    stopped_maintenance retains the selected trusted stop and the original PAUSED
    cron; it requires original execution readback and never makes approval ready.
    """
    root = Path(target_root).expanduser().absolute()
    if (supervisor_loss_source_only or official_stop_source_only or user_stop_source_only) and not stopped_maintenance:
        raise RuntimeError('production_release_stopped_maintenance_required')
    if sum((supervisor_loss_source_only, official_stop_source_only, user_stop_source_only)) > 1:
        raise RuntimeError('production_release_source_only_modes_conflict')
    if not _REVISION.fullmatch(expected_revision):
        raise RuntimeError('production_release_revision_invalid')
    maintenance_runtime.assert_target_environment(root)
    controls = None
    controls_before = None
    if stopped_maintenance:
        if automation is None:
            raise RuntimeError('production_release_automation_required')
        if original_execution is None:
            raise RuntimeError('production_release_original_execution_required')
        controls = _stopped_controls(root, automation, official_stop_source_only=official_stop_source_only,
                                     user_stop_source_only=user_stop_source_only)
        controls_before = {'stop': _control_proof(controls[0]), 'automation': _control_proof(controls[1]),
                           'automation_state': 'PAUSED'}
        if official_stop_source_only:
            controls_before['cooldown'] = _control_proof(controls[2])
    else:
        _assert_no_stop(root)
    def assert_controls() -> None:
        if stopped_maintenance:
            if _stopped_controls(root, automation, official_stop_source_only=official_stop_source_only,
                                     user_stop_source_only=user_stop_source_only) != controls:
                raise RuntimeError('production_release_stopped_controls_changed')
        else:
            _assert_no_stop(root)
    before = _source(root, expected_revision, expected_origin)
    if check_configuration is None:
        if automation is None:
            raise RuntimeError('production_release_automation_required')
        def configuration(target: Path, revision: str) -> list[str]:
            return [*candidate_configuration_findings(target, revision), *automation.findings(target)]
    else:
        configuration = check_configuration
    findings = configuration(root, expected_revision)
    if findings:
        raise RuntimeError('production_release_configuration_invalid:' + ','.join(findings))
    loss_proof = None
    official_proof = None
    user_stop_proof = None
    live = check_live or (lambda target: maintenance_runtime.read_live_status(target,
        require_reaped=stopped_maintenance, source_only_proof=loss_proof, official_stop_proof=official_proof))
    manifest_file = root / 'runtime/control/rolling-controller-manifest.json'
    manifest = runtime_control.read_control_bytes(manifest_file)
    if manifest is None:
        raise RuntimeError('production_release_manifest_missing')
    sync_started = False
    def original_readback() -> dict:
        if check_original is not None:
            report = check_original(root, original_execution)
        elif user_stop_source_only:
            from .owner_restart_recovery import _read_original
            report = _read_original(root, original_execution, manifest, user_stop=True, source_only=True)
        else:
            report = maintenance_runtime.read_original_execution(root, original_execution,
                supervisor_loss_source_only=supervisor_loss_source_only, official_stop_source_only=official_stop_source_only)
        maintenance_runtime.assert_original_execution_safe(root, original_execution, report, manifest,
            supervisor_loss_source_only=supervisor_loss_source_only, official_stop_source_only=official_stop_source_only)
        if loss_proof is not None and report.get('source_only_proof') != loss_proof:
            raise RuntimeError('production_release_original_evidence_changed')
        if official_proof is not None and report.get('official_stop_proof') != official_proof:
            raise RuntimeError('production_release_original_evidence_changed')
        if user_stop_proof is not None and report.get('owner_restart_proof') != user_stop_proof:
            raise RuntimeError('production_release_original_evidence_changed')
        return report
    try:
        with maintenance_runtime.target_runtime_leases(root):
            assert_controls()
            if stopped_maintenance:
                report = original_readback()
                if supervisor_loss_source_only:
                    loss_proof = report['source_only_proof']
                if official_stop_source_only:
                    official_proof = report['official_stop_proof']
                if user_stop_source_only:
                    user_stop_proof = report['owner_restart_proof']
            snapshot = live(root)
            maintenance_runtime.assert_live_safe(snapshot, source_only_proof=loss_proof, official_stop_proof=official_proof)
            pre_sync_status = snapshot.get('maintenance_status_file')
            if runtime_control.read_control_bytes(manifest_file) != manifest:
                raise RuntimeError('production_release_manifest_changed')
            if _source(root, expected_revision, expected_origin) != before:
                raise RuntimeError('production_release_source_changed')
            assert_controls()
            findings = configuration(root, expected_revision)
            if findings:
                raise RuntimeError('production_release_configuration_invalid:' + ','.join(findings))
            if supervisor_loss_source_only or official_stop_source_only or user_stop_source_only:
                original_readback()
            if confirmed_sync:
                assert_controls()
                sync_started = True
                _git(root, 'merge', '--ff-only', expected_revision)
                snapshot = live(root)
                maintenance_runtime.assert_live_safe(snapshot, source_only_proof=loss_proof, official_stop_proof=official_proof)
                if check_configuration is None:
                    from . import production_config
                    findings = production_config.repository_findings(root)
                    if findings:
                        raise RuntimeError('production_release_configuration_invalid:' + ','.join(findings))
            assert_controls()
            after = _source(root, expected_revision, expected_origin, synchronized=confirmed_sync)
            findings = configuration(root, expected_revision)
            if findings:
                raise RuntimeError('production_release_configuration_invalid:' + ','.join(findings))
            if runtime_control.read_control_bytes(manifest_file) != manifest:
                raise RuntimeError('production_release_manifest_changed')
            if stopped_maintenance:
                report = original_readback()
                assert_controls()
                stopped_result = _stopped_receipt(root, automation, controls_before,
                    official_stop_source_only=official_stop_source_only)
                if (stopped_result['stopped_controls_after'] != controls_before
                        or not stopped_result['stop_preserved'] or not stopped_result['automation_preserved']):
                    raise RuntimeError('production_release_stopped_controls_changed')
            return {
                'schema_version': 1, 'kind': 'production_source_release',
                'status': 'synced' if confirmed_sync else 'ready',
                'before_revision': before, 'revision': after, 'after_revision': after,
                'requested_revision': expected_revision,
                'source_sync_state': 'updated' if after != before else 'not_updated',
                'expected_revision': expected_revision, 'write_state': 'not_requested',
                'source_sync_requested': confirmed_sync,
                'next_process_ready': confirmed_sync and not stopped_maintenance,
                'production_acceptance': 'not_verified',
                'status_file': snapshot.get('maintenance_status_file'),
                'current_input_proof': snapshot.get('maintenance_current_inputs'),
                'continuation': snapshot['reconciliation']['continuation'],
                **({'supervisor_loss_source_only': True, 'original_wait_verified': False,
                    'approval_recovery_allowed': False, 'original_evidence_preserved': True}
                    if supervisor_loss_source_only else {}),
                **({'user_stop_source_only': True, 'original_wait_verified': True,
                    'approval_recovery_allowed': False, 'original_evidence_preserved': True}
                    if user_stop_source_only else {}),
                **({'official_stop_source_only': True, 'original_wait_verified': True,
                    'approval_recovery_allowed': False, 'original_evidence_preserved': True,
                    'pre_sync_status_file': pre_sync_status} if official_stop_source_only else {}),
                **({**stopped_result,
                    'original_execution': report} if stopped_maintenance else {}),
            }
    except (OSError, ValueError, RuntimeError) as error:
        if not sync_started:
            raise
        return _failed_release(root, before, expected_revision, confirmed_sync, error,
            stopped_maintenance=stopped_maintenance, automation=automation, controls_before=controls_before,
            supervisor_loss_source_only=supervisor_loss_source_only, official_stop_source_only=official_stop_source_only,
            user_stop_source_only=user_stop_source_only)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-root', required=True, type=Path)
    parser.add_argument('--expected-revision', required=True)
    parser.add_argument('--confirm-sync', choices=('YES',))
    parser.add_argument('--automation-file', required=True, type=Path)
    parser.add_argument('--production-project-id', required=True)
    parser.add_argument('--expected-schedule', required=True)
    parser.add_argument('--stopped-maintenance', choices=('YES',))
    parser.add_argument('--supervisor-loss-source-only', choices=('YES',))
    parser.add_argument('--official-stop-source-only', choices=('YES',))
    parser.add_argument('--user-stop-source-only', choices=('YES',))
    parser.add_argument('--original-receipt-file', type=Path)
    parser.add_argument('--original-owner-task-id')
    parser.add_argument('--original-tool-session-id')
    args = parser.parse_args(argv)
    before = _observed_revision(args.target_root)
    controls_before = None
    try:
        automation = ProductionAutomation(args.automation_file,
                                          args.production_project_id, args.expected_schedule)
        if args.stopped_maintenance == 'YES':
            controls_before = _stopped_observation(args.target_root, automation,
                official_stop_source_only=args.official_stop_source_only == 'YES')
        original = None
        if args.stopped_maintenance == 'YES':
            if not all((args.original_receipt_file, args.original_owner_task_id, args.original_tool_session_id)):
                raise RuntimeError('production_release_original_execution_required')
            original = maintenance_runtime.OriginalExecution(args.original_receipt_file,
                args.original_owner_task_id, args.original_tool_session_id)
        payload = release_source(args.target_root, expected_revision=args.expected_revision,
                                 confirmed_sync=args.confirm_sync == 'YES', automation=automation,
                                 stopped_maintenance=args.stopped_maintenance == 'YES', original_execution=original,
                                 supervisor_loss_source_only=args.supervisor_loss_source_only == 'YES',
                                 official_stop_source_only=args.official_stop_source_only == 'YES',
                                 user_stop_source_only=args.user_stop_source_only == 'YES')
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps(_failed_release(args.target_root, before, args.expected_revision, args.confirm_sync == 'YES', error,
            stopped_maintenance=args.stopped_maintenance == 'YES', automation=automation, controls_before=controls_before,
            supervisor_loss_source_only=args.supervisor_loss_source_only == 'YES',
            official_stop_source_only=args.official_stop_source_only == 'YES',
            user_stop_source_only=args.user_stop_source_only == 'YES')))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 1 if payload['status'] == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
