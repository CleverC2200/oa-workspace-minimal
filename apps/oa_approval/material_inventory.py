from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess


MATERIAL_DIRECTORIES = {
    'attachments', 'prepared', 'raw', 'compression', 'compressed', 'downloads',
    'extracted', 'rendered', 'finance_preaudit_attachments', 'finance_preaudit_prepared',
    'image-compressions',
}
EXCLUDED_DIRECTORIES = {'.git', '.venv', '__pycache__', '.pytest_cache', '.ruff_cache', 'node_modules'}


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', *arguments], cwd=root,
                            capture_output=True, text=True, check=False,
                            env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))
    if result.returncode:
        raise ValueError('project_space_checkout_unavailable')
    return result.stdout.strip()


def canonical_system_path(path: Path) -> Path:
    path = path.absolute()
    for alias, canonical in (('/tmp', '/private/tmp'), ('/var', '/private/var')):
        prefix = Path(alias)
        if path.is_relative_to(prefix) and prefix.is_symlink() and prefix.resolve() == Path(canonical):
            return Path(canonical) / path.relative_to(prefix)
    return path


def trusted_path(path: Path) -> Path:
    path = canonical_system_path(path)
    if '..' in path.parts or any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('project_space_path_untrusted')
    return path


def checkouts(root: Path) -> list[Path]:
    root = trusted_path(root)
    common = Path(_git(root, 'rev-parse', '--git-common-dir'))
    owner = (root / common).resolve().parent
    if owner.name not in {'oa-workspace-dev', 'oa-workspace-minimal'}:
        raise ValueError('project_space_checkout_role_unknown')
    roots = {owner}
    for line in _git(owner, 'worktree', 'list', '--porcelain').splitlines():
        if line.startswith('worktree '):
            roots.add(trusted_path(Path(line.removeprefix('worktree '))))
    sibling = owner.parent / ('oa-workspace-minimal' if owner.name == 'oa-workspace-dev' else 'oa-workspace-dev')
    if (sibling / '.git').exists():
        sibling_common = Path(_git(sibling, 'rev-parse', '--git-common-dir'))
        if (sibling / sibling_common).resolve().parent == sibling:
            roots.add(trusted_path(sibling))
            for line in _git(sibling, 'worktree', 'list', '--porcelain').splitlines():
                if line.startswith('worktree '):
                    roots.add(trusted_path(Path(line.removeprefix('worktree '))))
    return sorted(roots)


def scan_files(directory: Path, *, all_material: bool, allowed_roots: list[Path] | None = None) -> set[Path]:
    found = set()
    allowed_roots = allowed_roots or [directory]
    if not directory.exists():
        return found
    trusted_path(directory)
    for current, directories, files in os.walk(directory, followlinks=False):
        base = Path(current)
        directories[:] = [name for name in directories if name not in EXCLUDED_DIRECTORIES
                          and not (base / name / '.git').exists()]
        for name in list(directories):
            path = base / name
            if path.is_symlink():
                if not any(path.resolve().is_relative_to(root) for root in allowed_roots):
                    raise ValueError('project_space_path_untrusted')
                directories.remove(name)
            else:
                trusted_path(path)
        for name in files:
            path = base / name
            if path.is_symlink():
                if not any(path.resolve().is_relative_to(root) for root in allowed_roots):
                    raise ValueError('project_space_path_untrusted')
                continue
            path = trusted_path(path)
            mode = path.stat().st_mode
            if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ValueError('project_space_path_untrusted')
            if all_material or any(part in MATERIAL_DIRECTORIES for part in path.relative_to(directory).parts[:-1]):
                found.add(path)
    return found


def material_files(roots: list[Path]) -> set[Path]:
    found = set()
    for root in roots:
        found.update(scan_files(root / 'runtime', all_material=False, allowed_roots=roots))
        found.update(scan_files(root / '.scratch', all_material=True, allowed_roots=roots))
    return found


def default_checkout_root() -> Path:
    return Path(__file__).resolve().parents[2]


def discover(root: Path) -> dict:
    roots = checkouts(root)
    return {'checkouts': roots, 'files': material_files(roots),
            'scopes': {str(path): all_material for checkout in roots
                       for path, all_material in ((checkout / 'runtime', False), (checkout / '.scratch', True))}}
