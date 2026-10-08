"""One fail-closed gate for readonly release preflight and authorized source sync."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
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

    def findings(self) -> list[str]:
        from . import production_config
        raw = runtime_control.read_control_bytes(self.file)
        if raw is None:
            raise RuntimeError('production_release_automation_missing')
        configuration = tomllib.loads(raw.decode('utf-8'))
        return production_config.automation_findings(configuration,
            production_project_id=self.project_id, expected_schedule=self.schedule)


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


def release_source(
    target_root: Path, *, expected_revision: str, confirmed_sync: bool = False,
    expected_origin: str = ORIGIN, automation: ProductionAutomation | None = None,
    check_live: Callable[[Path], dict] | None = None,
    check_configuration: Callable[[Path, str], list[str]] | None = None,
) -> dict:
    """Preflight, optionally ff-only sync, and verify while retaining both leases.

    The default adapter reads target-local production state freshly. Injected
    adapters are for offline callers/tests; the CLI never accepts cached JSON.
    No OA write, stop mutation, automation change, fetch, dependency installation,
    or forced reset is performed. Fetch origin explicitly before calling.
    """
    root = Path(target_root).expanduser().absolute()
    if not _REVISION.fullmatch(expected_revision):
        raise RuntimeError('production_release_revision_invalid')
    maintenance_runtime.assert_target_environment(root)
    _assert_no_stop(root)
    before = _source(root, expected_revision, expected_origin)
    if check_configuration is None:
        if automation is None:
            raise RuntimeError('production_release_automation_required')
        def configuration(target: Path, revision: str) -> list[str]:
            return [*candidate_configuration_findings(target, revision), *automation.findings()]
    else:
        configuration = check_configuration
    findings = configuration(root, expected_revision)
    if findings:
        raise RuntimeError('production_release_configuration_invalid:' + ','.join(findings))
    live = check_live or maintenance_runtime.read_live_status
    manifest_file = root / 'runtime/control/rolling-controller-manifest.json'
    manifest = runtime_control.read_control_bytes(manifest_file)
    if manifest is None:
        raise RuntimeError('production_release_manifest_missing')
    with maintenance_runtime.target_runtime_leases(root):
        _assert_no_stop(root)
        snapshot = live(root)
        maintenance_runtime.assert_live_safe(snapshot)
        if runtime_control.read_control_bytes(manifest_file) != manifest:
            raise RuntimeError('production_release_manifest_changed')
        if _source(root, expected_revision, expected_origin) != before:
            raise RuntimeError('production_release_source_changed')
        _assert_no_stop(root)
        findings = configuration(root, expected_revision)
        if findings:
            raise RuntimeError('production_release_configuration_invalid:' + ','.join(findings))
        if confirmed_sync:
            _git(root, 'merge', '--ff-only', expected_revision)
            snapshot = live(root)
            maintenance_runtime.assert_live_safe(snapshot)
            if check_configuration is None:
                from . import production_config
                findings = production_config.repository_findings(root)
                if findings:
                    raise RuntimeError('production_release_configuration_invalid:' + ','.join(findings))
        _assert_no_stop(root)
        after = _source(root, expected_revision, expected_origin, synchronized=confirmed_sync)
        findings = configuration(root, expected_revision)
        if findings:
            raise RuntimeError('production_release_configuration_invalid:' + ','.join(findings))
        if runtime_control.read_control_bytes(manifest_file) != manifest:
            raise RuntimeError('production_release_manifest_changed')
        return {
            'schema_version': 1, 'kind': 'production_source_release',
            'status': 'synced' if confirmed_sync else 'ready',
            'before_revision': before, 'revision': after,
            'expected_revision': expected_revision, 'write_state': 'not_requested',
            'source_sync_requested': confirmed_sync,
            'next_process_ready': confirmed_sync,
            'production_acceptance': 'not_verified',
            'status_file': snapshot.get('maintenance_status_file'),
            'continuation': snapshot['reconciliation']['continuation'],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-root', required=True, type=Path)
    parser.add_argument('--expected-revision', required=True)
    parser.add_argument('--confirm-sync', choices=('YES',))
    parser.add_argument('--automation-file', required=True, type=Path)
    parser.add_argument('--production-project-id', required=True)
    parser.add_argument('--expected-schedule', required=True)
    args = parser.parse_args(argv)
    try:
        automation = ProductionAutomation(args.automation_file,
                                          args.production_project_id, args.expected_schedule)
        payload = release_source(args.target_root, expected_revision=args.expected_revision,
                                 confirmed_sync=args.confirm_sync == 'YES', automation=automation)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({'status': 'blocked', 'code': str(error), 'write_state': 'not_requested'}))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
