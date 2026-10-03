"""Read-only checkout preflight and explicit project-local development actions."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


class PrerequisiteError(ValueError):
    def __init__(self, reason, *, evidence=None):
        super().__init__(reason)
        self.evidence = evidence


def command(root, args, *, env=None):
    try:
        return subprocess.run(args, cwd=root, env=env, capture_output=True, text=True,
                              encoding='utf-8', errors='backslashreplace', check=False)
    except OSError as error:
        raise PrerequisiteError('command_unavailable', evidence={
            'command': args, 'exit_code': None, 'status': 'not_run', 'errno': error.errno}) from error


def git(root, *args):
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    result = command(root, ['git', '-c', 'core.fsmonitor=false', *args], env=environment)
    if result.returncode:
        raise PrerequisiteError('git_context_unavailable', evidence={
            'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
    return result.stdout.strip()


def requirements(root):
    try:
        if (root / 'requirements-dev.txt').is_symlink():
            raise PrerequisiteError('dependency_declaration_unsafe')
        raw = (root / 'requirements-dev.txt').read_bytes()
        pins = {}
        for line in raw.decode().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)', line)
            if not match:
                raise PrerequisiteError('dependency_declaration_unpinned')
            name, version = match.groups()
            if name in pins:
                raise PrerequisiteError('dependency_declaration_duplicate')
            pins[name] = version
        return pins, hashlib.sha256(raw).hexdigest()
    except (OSError, UnicodeError) as error:
        raise PrerequisiteError('dependency_declaration_unavailable') from error


def source_context(root, baseline, switch_to):
    if Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
        raise PrerequisiteError('checkout_root_required')
    common = Path(git(root, 'rev-parse', '--git-common-dir'))
    common = (root / common).resolve()
    owner = common.parent
    role = {'oa-workspace-dev': 'development', 'oa-workspace-minimal': 'production'}.get(owner.name, 'unknown')
    branch = git(root, 'branch', '--show-current')
    dirty = git(root, 'status', '--porcelain=v1', '-z')
    head = git(root, 'rev-parse', 'HEAD')
    try:
        baseline_sha = git(root, 'rev-parse', '--verify', baseline + '^{commit}')
        behind, ahead = map(int, git(root, 'rev-list', '--left-right', '--count', baseline_sha + '...HEAD').split())
        comparison = {'ref': baseline, 'head': baseline_sha, 'ahead': ahead, 'behind': behind}
    except PrerequisiteError:
        comparison = {'ref': baseline, 'status': 'missing', 'next_step': 'select_an_existing_fixed_baseline'}
    occupancy = []
    for entry in git(root, 'worktree', 'list', '--porcelain', '-z').split('\0\0'):
        fields = dict(field.split(' ', 1) for field in entry.split('\0') if ' ' in field)
        if fields.get('branch') == 'refs/heads/' + switch_to:
            occupancy.append(fields['worktree'])
    occupied_elsewhere = any(Path(path).resolve() != root for path in occupancy)
    return {'root': str(root), 'clone_root': str(owner), 'role': role, 'branch': branch or 'detached',
            'head': head, 'dirty': bool(dirty), 'changes': len([x for x in dirty.split('\0') if x]),
            'baseline': comparison, 'switch': {'branch': switch_to, 'occupied_elsewhere': occupied_elsewhere,
                                             'worktrees': occupancy, 'performed': False}}


# Inspect import paths without executing the application package or loading credentials.
PROBE = '''
import importlib.machinery, importlib.metadata, json, sys
pins = json.loads(sys.argv[1])
site = sys.argv[2]
sys.path.append(site)
versions = {}
locations = {}
files = []
for name in pins:
    try:
        dist = next(importlib.metadata.distributions(name=name, path=[site]))
        versions[name] = dist.version
        locations[name] = str(dist.locate_file(''))
        files.extend(str(dist.locate_file(path)) for path in (dist.files or []))
    except StopIteration: versions[name] = None
parent = importlib.machinery.PathFinder.find_spec('apps', sys.path)
module = importlib.machinery.PathFinder.find_spec('apps.oa_finance_preaudit', parent.submodule_search_locations) if parent and parent.submodule_search_locations else None
print(json.dumps({'executable':sys.executable,'prefix':sys.prefix,'base_prefix':sys.base_prefix,
                  'version':list(sys.version_info[:3]),'purelib':site,'probe_without_site':True,
                  'module_origin':module.origin if module else None,'versions':versions,
                  'dependency_locations':locations,'dependency_files':files}))
'''

# Verified against a fresh offline venv produced by the project's current uv 0.11.6.
# This bootstrap only patches distutils. A changed bootstrap requires inspection.
UV_BOOTSTRAP_SHA256 = '6cf30c56faf2a55228914dbbd17f8088ed371ebb08f5e7fa6fd931f913fcaf1d'


def environment_context(root):
    pins, digest = requirements(root)
    report = {'status': 'missing', 'requirements_sha256': digest, 'required_versions': pins,
              'next_step': 'run_explicit_prepare_action'}
    venv = root / '.venv'
    interpreter = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if venv.is_symlink():
        report.update(status='blocked', reason='shared_virtual_environment')
        return report
    if not interpreter.is_file():
        report['reason'] = 'interpreter_missing'
        return report
    probe = None
    try:
        cfg_path = venv / 'pyvenv.cfg'
        if cfg_path.is_symlink():
            report.update(status='blocked', reason='shared_virtual_environment')
            return report
        cfg = cfg_path.read_text().lower()
        purelib = venv / ('Lib/site-packages' if os.name == 'nt' else 'lib/python3.11/site-packages')
        if (not purelib.resolve().is_relative_to(venv.resolve())
                or re.search(r'include-system-site-packages\s*=\s*true', cfg)
                or any(path.is_symlink() and not path.resolve().is_relative_to(venv.resolve())
                       for path in purelib.rglob('*'))):
            report.update(status='blocked', reason='shared_writable_dependencies')
            return report
        hooks = list(purelib.glob('*.pth'))
        uv_hook = purelib / '_virtualenv.pth'
        uv_helper = purelib / '_virtualenv.py'
        if (uv_hook in hooks and not uv_hook.is_symlink() and not uv_helper.is_symlink()
                and uv_hook.read_bytes() == b'import _virtualenv' and uv_helper.is_file()
                and hashlib.sha256(uv_helper.read_bytes()).hexdigest() == UV_BOOTSTRAP_SHA256):
            hooks.remove(uv_hook)
        hooks.extend(path for path in (root / 'sitecustomize.py', root / 'usercustomize.py',
                                      purelib / 'sitecustomize.py', purelib / 'usercustomize.py') if path.exists())
        if hooks:
            report.update(status='blocked', reason='python_startup_hooks_require_inspection')
            return report
        probe_args = [str(interpreter), '-B', '-E', '-S', '-c', PROBE, json.dumps(pins), str(purelib)]
        probe_env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
        probe = command(root, probe_args, env=probe_env)
        if probe.returncode:
            report.update(status='blocked', reason='interpreter_probe_failed',
                          probe={'exit_code': probe.returncode, 'stdout': probe.stdout, 'stderr': probe.stderr})
            return report
        actual = json.loads(probe.stdout)
        report.update(actual)
        report['interpreter_realpath'] = str(interpreter.resolve())
        report['virtual_environment'] = str(venv)
        if (actual['executable'] != str(interpreter)
                or any(not Path(path).resolve().is_relative_to(venv.resolve())
                       for path in (*actual['dependency_locations'].values(), *actual['dependency_files']))):
            report.update(status='blocked', reason='shared_writable_dependencies')
        elif actual['version'][:2] != [3, 11]:
            report['reason'] = 'python_3_11_required'
        elif (not actual['module_origin'] or Path(actual['module_origin']).resolve()
              != root / 'apps/oa_finance_preaudit/__init__.py'):
            report.update(status='blocked', reason='module_origin_mismatch')
        elif any(actual['versions'].get(name) != version for name, version in pins.items()):
            report['reason'] = 'dependencies_missing_or_mismatched'
        else:
            report.update(status='ready')
            report.pop('next_step')
        report.pop('dependency_files', None)
    except (PrerequisiteError, OSError, ValueError, KeyError, TypeError) as error:
        report.update(status='blocked', reason='interpreter_probe_invalid')
        if probe is not None:
            report['probe'] = {'exit_code': probe.returncode, 'stdout': probe.stdout, 'stderr': probe.stderr}
        elif isinstance(error, PrerequisiteError) and error.evidence:
            report['probe'] = error.evidence
    return report


def preflight(root, *, baseline='origin/main', switch_to='main'):
    source = source_context(root, baseline, switch_to)
    environment = environment_context(root)
    tools = {name: shutil.which(name) is not None for name in ('git', 'uv', 'rtk')}
    reasons = []
    if source['role'] != 'development':
        reasons.append('development_clone_required')
    if source['baseline'].get('status') == 'missing':
        reasons.append('baseline_missing')
    if environment['status'] != 'ready':
        reasons.append(environment['reason'])
    if not all(tools.values()):
        reasons.append('development_tool_missing')
    if (root / 'runtime').is_symlink():
        reasons.append('shared_runtime_forbidden')
    return {'kind': 'oa_development_preflight', 'read_only': True, 'status': 'ready' if not reasons else 'missing_prerequisites',
            'reasons': reasons, 'source': source, 'environment': environment, 'tools': tools,
            'codegraph': {'project': str(root), 'branch': source['branch'], 'head': source['head'],
                          'freshness': 'not_verified', 'next_step': 'verify_index_for_this_revision_or_use_source_search'}}


def development_source(root, baseline):
    source = source_context(root, baseline, 'main')
    if source['role'] != 'development':
        raise PrerequisiteError('development_clone_required')
    if not source['branch'].startswith('codex/'):
        raise PrerequisiteError('development_branch_required')
    if (root / 'runtime').is_symlink():
        raise PrerequisiteError('shared_runtime_forbidden')
    if source['baseline'].get('status') == 'missing':
        raise PrerequisiteError('baseline_missing')
    return source


def tool_environment():
    # Do not pass business/model credentials or injected Python import paths to checks.
    return {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'TMPDIR', 'TMP', 'TEMP',
                                             'LANG', 'LC_ALL', 'UV_CACHE_DIR') if key in os.environ}


def run_check(root, name, args, environment):
    try:
        completed = command(root, args, env=environment)
        result = {'name': name, 'command': args, 'exit_code': completed.returncode,
                  'status': 'passed' if completed.returncode == 0 else 'failed'}
        if completed.returncode:
            result.update(stdout=completed.stdout, stderr=completed.stderr)
        elif name == 'tests':
            lines = completed.stdout.splitlines()
            if lines and re.fullmatch(r'\d+ passed(?:, \d+ [a-z]+)* in .+', lines[-1]):
                result['summary'] = lines[-1]
        return result
    except PrerequisiteError as error:
        return {'name': name, 'command': args, 'exit_code': None, 'status': 'not_run', 'reason': str(error)}


def exit_code(checks):
    for check in checks:
        if check['status'] != 'passed':
            code = check['exit_code']
            return code if isinstance(code, int) and 0 < code < 126 else 1
    return 0


def prepare(root, baseline):
    source = development_source(root, baseline)
    env = environment_context(root)
    if env['status'] == 'blocked':
        return {'kind': 'oa_dependency_preparation', 'status': 'missing_prerequisites',
                'source': source, 'environment': env, 'reasons': [env['reason']], 'read_only': True}, 2
    if env.get('reason') == 'python_3_11_required':
        raise PrerequisiteError('python_3_11_required')
    interpreter = root / '.venv/bin/python'
    if os.name == 'nt':
        interpreter = root / '.venv/Scripts/python.exe'
    checks = []
    environment = tool_environment()
    if not interpreter.is_file():
        if (root / '.venv').exists():
            raise PrerequisiteError('existing_virtual_environment_requires_inspection')
        checks.append(run_check(root, 'create_venv', ['uv', 'venv', '--python', '3.11', '.venv'], environment))
    if not exit_code(checks):
        checks.append(run_check(root, 'install_pins', ['uv', 'pip', 'install', '--python', str(interpreter),
                                                       '--requirement', 'requirements-dev.txt'], environment))
    code = exit_code(checks)
    if not code:
        env = environment_context(root)
        if env['status'] != 'ready':
            code = 2
    result = {'kind': 'oa_dependency_preparation', 'status': 'ready' if not code else 'failed',
              'read_only': False, 'source': source, 'environment': env, 'checks': checks}
    return result, code


QUALITY_CHECKS = ('skill', 'format', 'hygiene', 'links', 'yaml', 'schema', 'lint', 'environment')


def check_configuration(root):
    import tomllib
    prefix = '.venv/bin/python -B -m scripts.dev_environment '
    initial = 'python3 -B -m scripts.dev_environment '
    expected = {initial + 'preflight', initial + 'prepare',
                prefix + 'verify --check lint --check schema',
                prefix + 'verify --tests tests/finance_preaudit/test_dev_environment.py',
                prefix + 'verify --full'}
    try:
        value = tomllib.loads((root / '.codex/environments/environment.toml').read_text())
        actions = value['actions']
        if (set(value) != {'version', 'name', 'setup', 'actions'} or value['version'] != 1
                or value['name'] != 'oa-workspace-dev' or value['setup'] != {'script': initial + 'preflight'}
                or not isinstance(actions, list) or len(actions) != len(expected)
                or any(set(action) != {'name', 'icon', 'command'} or action['icon'] != 'tool'
                       or not isinstance(action['name'], str) or not action['name'].strip() for action in actions)
                or len({action['name'] for action in actions}) != len(actions)
                or {action['command'] for action in actions} != expected):
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError):
        return ['native_environment_invalid']
    return []


def resource_check(root):
    free = shutil.disk_usage(root).free
    processes = command(root, ['ps', '-Ao', 'args='])
    if processes.returncode:
        raise PrerequisiteError('process_inspection_unavailable', evidence={
            'exit_code': processes.returncode, 'stdout': processes.stdout, 'stderr': processes.stderr})
    active = sum(any(Path(token).name == 'finance_preaudit_run.py'
                     or token == 'apps.oa_finance_preaudit.controller_cli'
                     for token in line.split()) for line in processes.stdout.splitlines())
    if free < 2 * 1024**3:
        raise PrerequisiteError('insufficient_disk_for_full_validation')
    if active:
        raise PrerequisiteError('active_controller_defer_full_validation')
    return {'free_bytes': free, 'active_controllers': active}


def verify(root, baseline, checks, tests, full):
    source = development_source(root, baseline)
    for name in ('.env', '.env.oa-auth'):
        if os.path.lexists(root / name):
            raise PrerequisiteError('local_credentials_present')
    flight = preflight(root, baseline=baseline)
    if flight['status'] != 'ready':
        return flight, 2
    started = datetime.now(timezone.utc).isoformat()
    selected = list(QUALITY_CHECKS) if full else list(dict.fromkeys(checks or ('lint', 'schema')))
    commands = [(name, ['-m', 'scripts.quality', name]) for name in selected]
    if full:
        commands.append(('syntax', ['-m', 'compileall', '-q', 'apps', 'scripts', 'tests']))
    if tests or full:
        for name in tests:
            path = Path(name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'tests' or not (root / path).exists():
                raise PrerequisiteError('project_test_path_required')
        commands.append(('tests', ['-m', 'pytest', '-q', '--tb=short', *(tests if not full else [])]))
    resources = resource_check(root) if full else {'status': 'not_requested'}
    environment = tool_environment()
    environment.update(PYTHONDONTWRITEBYTECODE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    outcomes = []
    with tempfile.TemporaryDirectory(prefix='oa-offline-checks-') as temporary:
        # Empty private auth source and a temporary HOME prevent stable account/tool fallback.
        auth_file = Path(temporary) / 'auth.env'
        auth_file.touch(mode=0o600)
        environment.update(HOME=temporary, OA_AUTH_ENV_FILE=str(auth_file))
        interpreter = flight['environment']['executable']
        for name, arguments in commands:
            outcomes.append(run_check(root, name, ['rtk', 'proxy', interpreter, '-B', *arguments], environment))
    code = exit_code(outcomes)
    result = {'kind': 'oa_development_verification', 'status': 'ready' if not code else 'failed',
              'source': source, 'environment': flight['environment'], 'codegraph': flight['codegraph'],
              'resources': resources, 'checks': outcomes, 'offline_credentials': 'isolated',
              'started_at': started, 'reusable': not source['dirty'] and not code}
    return result, code


def emit(report, as_json):
    if as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return
    source = report.get('source', {})
    print(f"{report['kind']}: {report['status']}")
    if source:
        print(f"{source['role']} {source['root']} | {source['branch']} {source['head']} | dirty={source['dirty']}")
        base = source['baseline']
        print(f"baseline={base['ref']} {base.get('head', 'missing')} ahead={base.get('ahead', '?')} behind={base.get('behind', '?')}")
        if source['switch']['occupied_elsewhere']:
            print(f"switch {source['switch']['branch']}: occupied elsewhere; no switch performed")
    environment = report.get('environment', {})
    if environment:
        print(f"environment={environment['status']} | interpreter={environment.get('executable', 'missing')} | requirements={environment['requirements_sha256']}")
        if 'probe' in environment:
            print(f"interpreter probe: exit={environment['probe']['exit_code']}")
            for stream in ('stdout', 'stderr'):
                if environment['probe'].get(stream):
                    print(environment['probe'][stream], end='')
    for reason in report.get('reasons', []):
        print(reason)
    if report.get('failure_evidence'):
        evidence = report['failure_evidence']
        print(f"prerequisite: exit={evidence['exit_code']}")
        for stream in ('stdout', 'stderr'):
            if evidence.get(stream):
                print(evidence[stream], end='')
    for check in report.get('checks', []):
        print(f"{check['name']}: {check['status']} exit={check['exit_code']}")
        for stream in ('stdout', 'stderr'):
            if check.get(stream):
                print(check[stream], end='' if check[stream].endswith('\n') else '\n')
    if report.get('status') != 'ready':
        print('next: inspect prerequisites; use the explicit dependency action in a development clone')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--json', action='store_true')
    commands = parser.add_subparsers(dest='action', required=True)
    check = commands.add_parser('preflight')
    check.add_argument('--baseline', default='origin/main')
    check.add_argument('--switch-to', default='main')
    setup = commands.add_parser('prepare')
    setup.add_argument('--baseline', default='origin/main')
    validation = commands.add_parser('verify')
    validation.add_argument('--baseline', default='origin/main')
    validation.add_argument('--check', choices=QUALITY_CHECKS, action='append', default=[])
    validation.add_argument('--tests', action='append', default=[])
    validation.add_argument('--full', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action == 'prepare':
            result, code = prepare(args.root.resolve(), args.baseline)
        elif args.action == 'verify':
            result, code = verify(args.root.resolve(), args.baseline, args.check, args.tests, args.full)
        else:
            result = preflight(args.root.resolve(), baseline=args.baseline, switch_to=args.switch_to)
            code = 0 if result['status'] == 'ready' else 2
    except PrerequisiteError as error:
        kind = {'preflight': 'oa_development_preflight', 'prepare': 'oa_dependency_preparation',
                'verify': 'oa_development_verification'}[args.action]
        result = {'kind': kind, 'status': 'missing_prerequisites', 'reasons': [str(error)], 'read_only': True}
        if error.evidence:
            result['failure_evidence'] = error.evidence
        code = 2
    emit(result, args.json)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
