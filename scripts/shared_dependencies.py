from __future__ import annotations

import ast
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys


class DependencyError(ValueError):
    pass


def marker_matches(expression):
    values = {'sys_platform': os.sys.platform, 'platform_python_implementation': 'CPython',
              'implementation_name': 'cpython'}

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Name) and node.id in values:
            return values[node.id]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
            left, right = evaluate(node.left), evaluate(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            operands = [evaluate(value) for value in node.values]
            return all(operands) if isinstance(node.op, ast.And) else any(operands)
        raise DependencyError('dependency_lock_marker_unsupported')

    try:
        return evaluate(ast.parse(expression, mode='eval'))
    except SyntaxError as error:
        raise DependencyError('dependency_lock_marker_invalid') from error


def read_pins(path, *, markers=False):
    if path.is_symlink():
        raise DependencyError('dependency_declaration_unsafe')
    try:
        raw = path.read_bytes()
        pins = {}
        for line in raw.decode().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)(?:\s*;\s*(.+))?', line)
            if not match or (match.group(3) and not markers):
                raise DependencyError('dependency_declaration_unpinned')
            name, version, marker = match.groups()
            if marker and not marker_matches(marker):
                continue
            name = re.sub(r'[-_.]+', '-', name).lower()
            if name in pins:
                raise DependencyError('dependency_declaration_duplicate')
            pins[name] = version
        return pins, hashlib.sha256(raw).hexdigest()
    except (OSError, UnicodeError) as error:
        raise DependencyError('dependency_declaration_unavailable') from error


def plan(root):
    direct, declaration_digest = read_pins(root / 'requirements-dev.txt')
    pins, digest = read_pins(root / 'requirements-dev.lock', markers=True)
    if any(pins.get(name) != version for name, version in direct.items()):
        raise DependencyError('dependency_lock_out_of_date')
    python = sys._base_executable if sys.version_info[:2] == (3, 11) else shutil.which('python3.11')
    if not python:
        raise DependencyError('python_3_11_required')
    environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
    result = subprocess.run([python, '-B', '-E', '-S', '-c',
                             'import json,sys;print(json.dumps({"executable":sys.executable,"version":list(sys.version_info[:3]),"cache_tag":sys.implementation.cache_tag}))'],
                            capture_output=True, text=True, env=environment, check=False)
    try:
        identity = json.loads(result.stdout)
        if (result.returncode or identity['version'][:2] != [3, 11]
                or not identity['cache_tag'].startswith('cpython-')):
            raise ValueError
        identity['executable'] = str(Path(identity['executable']).resolve())
    except (ValueError, KeyError, TypeError) as error:
        raise DependencyError('python_3_11_required') from error
    identity.update(lock_sha256=digest,
                    platform=platform.system(), machine=platform.machine(), uv=direct.get('uv', '0.11.6'))
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    base = Path(os.environ.get('OA_DEVELOPMENT_ENV_ROOT', str(Path.home() / '.cache/oa-workspace/dependencies'))).absolute()
    if (any(path.is_symlink() or (path / '.git').exists() for path in (base, *base.parents))
            or base.resolve().is_relative_to(root.resolve())):
        raise DependencyError('shared_dependency_root_unsafe')
    return {'required_versions': pins, 'requirements_sha256': declaration_digest, 'lock_sha256': digest,
            'identity': identity, 'key': key, 'base': base, 'path': base / key}


@contextmanager
def preparation_lock(base):
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if base.stat().st_uid != os.getuid() or base.stat().st_mode & 0o077:
        raise DependencyError('shared_dependency_root_permissions')
    lock = base / '.prepare.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def seal(path, identity):
    (path / '.oa-environment.json').write_text(json.dumps(identity, sort_keys=True))
    for item in sorted(path.rglob('*'), key=lambda value: len(value.parts), reverse=True):
        if not item.is_symlink():
            item.chmod(stat.S_IMODE(item.stat().st_mode) & ~0o222)
    path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)


def validate(path, identity):
    try:
        marker = path / '.oa-environment.json'
        if path.is_symlink() or marker.is_symlink() or json.loads(marker.read_text()) != identity:
            return 'shared_dependency_manifest_invalid'
        if any(item.stat().st_mode & 0o222 for item in (path, *path.rglob('*')) if not item.is_symlink()):
            return 'shared_dependencies_writable'
    except (OSError, ValueError):
        return 'shared_dependency_manifest_invalid'
    return None
