from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess


MATERIAL_DIRECTORIES = {
    'attachments', 'prepared', 'raw', 'compression', 'compressed', 'downloads',
    'extracted', 'rendered', 'finance_preaudit_attachments',
}
EXCLUDED_DIRECTORIES = {'.git', '.venv', '__pycache__', '.pytest_cache', '.ruff_cache', 'node_modules'}


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', *arguments], cwd=root,
                            capture_output=True, text=True, check=False,
                            env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))
    if result.returncode:
        raise ValueError('project_space_checkout_unavailable')
    return result.stdout.strip()


def _trusted(path: Path) -> Path:
    path = path.absolute()
    if '..' in path.parts or any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('project_space_path_untrusted')
    return path


def checkouts(root: Path) -> list[Path]:
    root = _trusted(root)
    common = Path(_git(root, 'rev-parse', '--git-common-dir'))
    owner = (root / common).resolve().parent
    if owner.name not in {'oa-workspace-dev', 'oa-workspace-minimal'}:
        raise ValueError('project_space_checkout_role_unknown')
    roots = {owner}
    for line in _git(owner, 'worktree', 'list', '--porcelain').splitlines():
        if line.startswith('worktree '):
            roots.add(_trusted(Path(line.removeprefix('worktree '))))
    sibling = owner.parent / ('oa-workspace-minimal' if owner.name == 'oa-workspace-dev' else 'oa-workspace-dev')
    if (sibling / '.git').exists():
        sibling_common = Path(_git(sibling, 'rev-parse', '--git-common-dir'))
        if (sibling / sibling_common).resolve().parent == sibling:
            roots.add(_trusted(sibling))
            for line in _git(sibling, 'worktree', 'list', '--porcelain').splitlines():
                if line.startswith('worktree '):
                    roots.add(_trusted(Path(line.removeprefix('worktree '))))
    return sorted(roots)


def _files(directory: Path, *, all_material: bool) -> set[Path]:
    found = set()
    if not directory.exists():
        return found
    _trusted(directory)
    for current, directories, files in os.walk(directory, followlinks=False):
        base = Path(current)
        directories[:] = [name for name in directories if name not in EXCLUDED_DIRECTORIES
                          and not (base / name / '.git').exists()]
        for name in directories:
            _trusted(base / name)
        for name in files:
            path = _trusted(base / name)
            if not path.is_file():
                raise ValueError('project_space_path_untrusted')
            if all_material or any(part in MATERIAL_DIRECTORIES for part in path.relative_to(directory).parts[:-1]):
                found.add(path)
    return found


def material_files(roots: list[Path]) -> set[Path]:
    found = set()
    for root in roots:
        found.update(_files(root / 'runtime', all_material=False))
        found.update(_files(root / '.scratch', all_material=True))
    return found


def _preflight(root: Path) -> dict:
    from apps.oa_approval.material_store import MaterialStore
    store = MaterialStore()
    roots = checkouts(root)
    files = material_files(roots)
    registered = {Path(entry['path']) for entry in store.registered_files() if entry['status'] != 'retired'}
    unregistered = files - registered
    report = store.report()
    uncovered = sum(path.stat().st_size for path in unregistered)
    observed = report['used_bytes'] + uncovered
    reasons = []
    if unregistered:
        reasons.append('project_materials_unregistered')
    if observed > report['limit_bytes']:
        reasons.append('project_materials_over_budget')
    return {'kind': 'oa_project_space', 'read_only': True, **report,
            'observed_bytes': observed, 'unregistered_bytes': uncovered,
            'unregistered_count': len(unregistered), 'checkout_count': len(roots),
            'coverage': 'incomplete' if unregistered else 'complete',
            'status': 'blocked' if reasons else 'ready', 'reasons': reasons,
            'shell_writes_quota_enforced': False}


def _reason(error: Exception) -> str:
    reason = str(error)
    return reason if re.fullmatch(r'[a-z][a-z0-9_]+', reason) else 'project_space_operation_failed'


def preflight(root: Path) -> dict:
    try:
        return _preflight(root)
    except (OSError, ValueError) as error:
        return {'kind': 'oa_project_space', 'read_only': True, 'status': 'blocked',
                'coverage': 'unknown', 'reasons': [_reason(error)]}


def _experiment(store, name: str) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', name):
        raise ValueError('material_workspace_invalid')
    directory = _trusted(store.root / 'experiments' / name)
    if str(directory) not in store.registered_directories():
        raise ValueError('material_workspace_unregistered')
    return directory


def check_policy(root: Path) -> list[str]:
    findings = []
    policy = 'docs/agents/project-space.md'
    if not (root / policy).is_file() or policy not in (root / 'AGENTS.md').read_text():
        findings.append('project_space_policy_pointer_missing')
    workflow = (root / '.github/workflows/quality.yml').read_text()
    if 'python -m scripts.quality storage' not in workflow:
        findings.append('project_space_ci_check_missing')
    config = (root / '.codex/environments/environment.toml').read_text()
    for command in ('scripts.project_space status', 'scripts.project_space experiment'):
        if command not in config:
            findings.append('project_space_action_missing:' + command.rsplit(' ', 1)[-1])
    for folder in ('apps', 'scripts'):
        for path in (root / folder).rglob('*.py'):
            if path == root / 'scripts/project_space.py':
                continue
            tree = ast.parse(path.read_text())
            if any(isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and '.scratch' in Path(node.value).parts for node in ast.walk(tree)):
                findings.append('unmanaged_scratch_path:' + str(path.relative_to(root)))
    for relative in ('apps/oa_approval/attachments.py', 'apps/oa_approval/attachment_extractors.py',
                     'apps/oa_approval/image_compression.py', 'apps/oa_approval/overseas_invoice.py',
                     'apps/oa_finance_preaudit/flow_engine.py'):
        tree = ast.parse((root / relative).read_text())
        if not any(isinstance(node, ast.Name) and node.id in {'material_store', 'MaterialStore'}
                   and isinstance(node.ctx, ast.Load) for node in ast.walk(tree)):
            findings.append('material_budget_entrypoint_missing:' + relative)
    environment = ast.parse((root / 'scripts/dev_environment.py').read_text())
    if not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and isinstance(node.func.value, ast.Name) and node.func.value.id == 'project_space'
               and node.func.attr == 'preflight' for node in ast.walk(environment)):
        findings.append('project_space_preflight_missing')
    return sorted(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='oa-project-space')
    parser.add_argument('--root', type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    commands.add_parser('register-checkout')
    experiment = commands.add_parser('experiment')
    experiment.add_argument('--name', required=True)
    importer = commands.add_parser('import')
    importer.add_argument('--name', required=True)
    importer.add_argument('--file', type=Path, required=True)
    importer.add_argument('--filename', required=True)
    finish = commands.add_parser('finish')
    finish.add_argument('--name', required=True)
    commands.add_parser('reclaim')
    args = parser.parse_args(argv)
    from apps.oa_approval.material_store import MaterialStore
    try:
        store = MaterialStore()
        if args.command == 'status':
            report = preflight(args.root)
        elif args.command == 'register-checkout':
            files = material_files(checkouts(args.root))
            registered = store.adopt_files(sorted(files), source={'kind': 'legacy'}, protected=True)
            report = {'kind': 'oa_material_registration', **registered}
        elif args.command == 'experiment':
            checkouts(args.root)
            report = {'kind': 'oa_experiment_workspace', 'directory': str(store.managed_directory(args.name)),
                      'quota_enforced_import': 'python -m scripts.project_space import',
                      'shell_writes_quota_enforced': False}
        elif args.command == 'import':
            admission = preflight(args.root)
            if admission['reasons']:
                raise ValueError(admission['reasons'][0])
            directory = _experiment(store, args.name)
            if Path(args.filename).name != args.filename or args.filename in {'.', '..'}:
                raise ValueError('material_filename_invalid')
            source = _trusted(args.file)
            with source.open('rb') as stream:
                target = store.copy_stream(directory / args.filename, stream,
                                           source={'kind': 'experiment'}, expected_bytes=source.stat().st_size)
            report = {'kind': 'oa_experiment_import', 'bytes': target.stat().st_size}
        elif args.command == 'finish':
            checkouts(args.root)
            directory = _experiment(store, args.name)
            files = _files(directory, all_material=True)
            for path in sorted(files):
                store.mark_disposable(path, closed=True, write_state='not_applicable', source={'kind': 'experiment'})
            report = {'kind': 'oa_experiment_finished', 'disposable_count': len(files)}
        else:
            checkouts(args.root)
            report = {'kind': 'oa_material_reclaim', **store.reclaim()}
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 1 if report.get('status') == 'blocked' else 0
    except (OSError, ValueError) as error:
        print(json.dumps({'kind': 'oa_project_space_error', 'reason': _reason(error)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
