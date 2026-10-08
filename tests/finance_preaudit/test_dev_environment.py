from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/dev_environment.py'


@pytest.fixture(autouse=True)
def command_adapters(tmp_path, monkeypatch):
    # Tool ports are controlled so clean CI needs neither uv installs nor RTK.
    tools = tmp_path / 'command-adapters'
    tools.mkdir()
    uv = tools / 'uv'
    uv.write_text('#!' + sys.executable + '\nraise SystemExit("unexpected install")\n')
    uv.chmod(0o700)
    rtk = tools / 'rtk'
    rtk.write_text('#!' + sys.executable + '\nimport subprocess, sys\n'
                   'assert sys.argv[1] == "proxy"\n'
                   'raise SystemExit(subprocess.run(sys.argv[2:]).returncode)\n')
    rtk.chmod(0o700)
    monkeypatch.setenv('PATH', str(tools) + os.pathsep + os.environ['PATH'])


def git(root, *args):
    return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args],
                          cwd=root, capture_output=True, text=True, check=True)


def checkout(tmp_path, name='oa-workspace-dev'):
    root = tmp_path / name
    root.mkdir()
    git(root, 'init', '-q', '-b', 'codex/test')
    (root / '.gitignore').write_text('.venv/\n__pycache__/\n')
    (root / 'requirements-dev.txt').write_text('')
    package = root / 'apps/oa_finance_preaudit'
    package.mkdir(parents=True)
    (package.parent / '__init__.py').write_text('')
    (package / '__init__.py').write_text('raise AssertionError("preflight must not import apps")\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'fixture')
    git(root, 'branch', 'main')
    return root


def venv(root):
    subprocess.run([sys.executable, '-B', '-m', 'venv', '--without-pip', str(root / '.venv')], check=True,
                   capture_output=True)


def run(root, *args, extra_env=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, '-B', str(SCRIPT), '--root', str(root), '--json', *args],
                          env=env, cwd=root, capture_output=True, text=True)


def test_native_preflight_is_readonly_and_reports_real_checkout(tmp_path):
    root = checkout(tmp_path)
    venv(root)
    dirty = root / 'requirements-dev.txt'
    dirty.write_text('# dirty fixture\n')
    (root / 'untracked.txt').write_text('keep me\n')
    refs = git(root, 'show-ref').stdout
    index = (root / '.git/index').read_bytes()
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file() and '.git' not in p.parts}
    result = run(root, 'preflight', '--baseline', 'main', '--switch-to', 'main')
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report['source']['role'] == 'development'
    assert report['source']['branch'] == 'codex/test'
    assert report['source']['head'] == git(root, 'rev-parse', 'HEAD').stdout.strip()
    assert report['source']['dirty'] is True
    assert report['environment']['status'] == 'ready'
    assert report['environment']['module_origin'] == str(root / 'apps/oa_finance_preaudit/__init__.py')
    assert report['codegraph']['freshness'] == 'not_verified'
    assert git(root, 'show-ref').stdout == refs
    assert (root / '.git/index').read_bytes() == index
    assert all(p.read_bytes() == value for p, value in before.items())


def test_concise_preflight_shows_actual_module_and_graph_source(tmp_path):
    root = checkout(tmp_path)
    venv(root)
    result = subprocess.run([sys.executable, '-B', str(SCRIPT), '--root', str(root),
                             'preflight', '--baseline', 'main'], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0
    assert 'module_origin=' + str(root / 'apps/oa_finance_preaudit/__init__.py') in result.stdout
    assert 'codegraph=not_verified | project=' + str(root) in result.stdout
    assert 'source_search' in result.stdout


@pytest.mark.parametrize('scenario,reason', [
    ('interpreter', 'interpreter_missing'), ('dependencies', 'dependencies_missing_or_mismatched'),
    ('shared_venv', 'shared_virtual_environment'), ('shared_packages', 'shared_writable_dependencies'),
    ('module_source', 'module_origin_mismatch'), ('production', 'development_clone_required'),
    ('unknown_role', 'development_clone_required'), ('baseline', 'baseline_missing'),
])
def test_preflight_reports_missing_prerequisites_without_repairs(tmp_path, scenario, reason):
    name = {'production':'oa-workspace-minimal', 'unknown_role':'unknown-clone'}.get(scenario, 'oa-workspace-dev')
    root = checkout(tmp_path, name)
    if scenario != 'interpreter':
        venv(root)
    if scenario == 'dependencies':
        (root / 'requirements-dev.txt').write_text('nonexistent-test-package==1.0\n')
    elif scenario == 'shared_venv':
        shared = tmp_path / 'shared'
        (root / '.venv').rename(shared)
        (root / '.venv').symlink_to(shared, target_is_directory=True)
    elif scenario == 'shared_packages':
        cfg = root / '.venv/pyvenv.cfg'
        cfg.write_text(cfg.read_text().replace('include-system-site-packages = false', 'include-system-site-packages = true'))
    elif scenario == 'module_source':
        outside = tmp_path / 'outside'
        (root / 'apps').rename(outside)
        (root / 'apps').symlink_to(outside, target_is_directory=True)
    before = git(root, 'status', '--porcelain').stdout
    result = run(root, 'preflight', '--baseline', 'missing' if scenario == 'baseline' else 'main')
    assert result.returncode == 2, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert reason in report['reasons']
    assert git(root, 'status', '--porcelain').stdout == before


def test_preflight_reports_occupied_branch_and_fixed_revision_difference(tmp_path):
    root = checkout(tmp_path)
    venv(root)
    other = tmp_path / 'occupied-main'
    git(root, 'worktree', 'add', '-q', str(other), 'main')
    (root / 'change.txt').write_text('new commit\n')
    git(root, 'add', 'change.txt')
    git(root, 'commit', '-qm', 'ahead')
    result = run(root, 'preflight', '--baseline', 'main', '--switch-to', 'main')
    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report['source']['baseline']['ahead'] == 1
    assert report['source']['baseline']['behind'] == 0
    assert report['source']['switch']['occupied_elsewhere'] is True
    assert report['source']['switch']['worktrees'] == [str(other)]
    assert git(other, 'branch', '--show-current').stdout.strip() == 'main'


def test_preflight_accepts_shared_interpreter_cache_but_not_writable_environment(tmp_path):
    root = checkout(tmp_path)
    venv(root)
    interpreter = root / '.venv/bin/python'
    assert interpreter.resolve().is_relative_to(root) is False
    result = run(root, 'preflight', '--baseline', 'main')
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)['environment']['status'] == 'ready'


def fake_uv(tmp_path):
    directory = tmp_path / 'tools'
    directory.mkdir()
    binary = directory / 'uv'
    binary.write_text('#!' + sys.executable + '\nimport json, pathlib, sys\n'
                      "p=pathlib.Path.cwd()/'uv-invocations.jsonl'\n"
                      "with p.open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                      "if sys.argv[1]=='venv':\n"
                      " import venv; venv.EnvBuilder(with_pip=False).create('.venv')\n"
                      "if sys.argv[1]=='pip': print('synthetic install failure', file=sys.stderr); sys.exit(17)\n")
    binary.chmod(0o700)
    return str(directory) + os.pathsep + os.environ['PATH']


def test_prepare_is_explicit_uses_uv_pins_and_preserves_failure(tmp_path):
    root = checkout(tmp_path)
    (root / 'requirements-dev.txt').write_text('pytest==9.1.1\n')
    tool_path = fake_uv(tmp_path)
    env = {'PATH':tool_path, 'OA_APP_SECRET':'synthetic-secret-must-not-leak'}
    assert run(root, 'preflight', '--baseline', 'main', extra_env=env).returncode == 2
    assert not (root / 'uv-invocations.jsonl').exists()
    result = run(root, 'prepare', '--baseline', 'main', extra_env=env)
    assert result.returncode == 17, result.stdout + result.stderr
    calls = [json.loads(line) for line in (root / 'uv-invocations.jsonl').read_text().splitlines()]
    assert calls[0] == ['venv', '--python', '3.11', '.venv']
    assert calls[1] == ['pip', 'install', '--python', str(root / '.venv/bin/python'), '--requirement', 'requirements-dev.txt']
    report = json.loads(result.stdout)
    assert report['checks'][-1]['exit_code'] == 17
    assert report['checks'][-1]['stderr'] == 'synthetic install failure\n'
    assert 'synthetic-secret' not in result.stdout + result.stderr
    assert (root / 'requirements-dev.txt').read_text() == 'pytest==9.1.1\n'


def test_prepare_never_runs_tools_on_production_or_shared_venv(tmp_path):
    root = checkout(tmp_path, 'oa-workspace-minimal')
    result = run(root, 'prepare', '--baseline', 'main', extra_env={'PATH':fake_uv(tmp_path)})
    assert result.returncode == 2
    assert not (root / 'uv-invocations.jsonl').exists()


def quality_fixture(root):
    scripts = root / 'scripts'
    scripts.mkdir()
    (scripts / '__init__.py').write_text('')
    (scripts / 'quality.py').write_bytes(SCRIPT.with_name('quality.py').read_bytes())
    (root / 'bad.txt').write_bytes(b'bad\r\n')


def test_verification_preserves_early_failure_and_later_success(tmp_path):
    root = checkout(tmp_path)
    venv(root)
    quality_fixture(root)
    result = run(root, 'verify', '--baseline', 'main', '--check', 'format', '--check', 'hygiene',
                 extra_env={'OA_APP_SECRET':'never-pass-this-secret'})
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert [(item['name'],item['exit_code'],item['status']) for item in report['checks']] == [
        ('format',1,'failed'),('hygiene',0,'passed')]
    assert report['checks'][0]['stdout'] == 'crlf:bad.txt\n'
    assert 'stdout' not in report['checks'][1]
    assert report['source']['dirty'] is True
    assert report['environment']['requirements_sha256']
    assert 'never-pass-this-secret' not in result.stdout
    assert report['status'] == 'failed'


def test_verify_blocks_local_auth_files_without_reading_them(tmp_path):
    root = checkout(tmp_path)
    venv(root)
    (root / '.env').write_text('arbitrary-private-value\n')
    result = run(root, 'verify', '--baseline', 'main', '--check', 'format')
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert 'local_credentials_present' in report['reasons']
    assert 'arbitrary-private-value' not in result.stdout + result.stderr
    assert 'checks' not in report


def test_native_configuration_is_checked_through_existing_quality_entry():
    from scripts import quality
    assert quality.main(['environment']) == 0


def test_native_setup_and_actions_execute_the_formal_entry(tmp_path):
    import shlex
    import tomllib
    configuration = tomllib.loads((SCRIPT.parents[1] / '.codex/environments/environment.toml').read_text())
    root = checkout(tmp_path)
    venv(root)
    scripts = root / 'scripts'
    scripts.mkdir()
    (scripts / '__init__.py').write_text('')
    (scripts / 'dev_environment.py').write_bytes(SCRIPT.read_bytes())
    setup = command_from_config = shlex.split(configuration['setup']['script'])
    setup[0] = sys.executable
    process = subprocess.run(command_from_config, cwd=root, capture_output=True, text=True)
    # No remote baseline exists in this synthetic checkout. Setup reports the gap without fetching.
    assert process.returncode == 2
    assert 'baseline_missing' in process.stdout
    assert configuration['setup']['script'].endswith(' preflight')
    assert any(action['command'].endswith(' prepare') for action in configuration['actions'])


def test_configuration_rejects_mutated_setup_and_invalid_actions(tmp_path):
    from scripts.dev_environment import check_configuration
    config = tmp_path / '.codex/environments/environment.toml'
    config.parent.mkdir(parents=True)
    original = (SCRIPT.parents[1] / '.codex/environments/environment.toml').read_text()
    for text in (original.replace('script = "python3 -B -m scripts.dev_environment preflight"',
                                  'script = "python3 -B -m scripts.dev_environment prepare"'),
                 original.replace('icon = "tool"', 'icon = []', 1), 'broken = ['):
        config.write_text(text)
        assert check_configuration(tmp_path) == ['native_environment_invalid']


def test_missing_tool_is_reported_and_does_not_install(tmp_path):
    import shutil
    root = checkout(tmp_path)
    venv(root)
    tools = tmp_path / 'git-only'
    tools.mkdir()
    (tools / 'git').symlink_to(shutil.which('git'))
    result = run(root, 'preflight', '--baseline', 'main', extra_env={'PATH':str(tools)})
    report = json.loads(result.stdout)
    assert result.returncode == 2
    assert 'development_tool_missing' in report['reasons']
    assert report['tools'] == {'git':True,'uv':False,'rtk':False}


def test_later_check_not_run_cannot_be_hidden_by_first_success(tmp_path):
    import shutil
    root = checkout(tmp_path)
    venv(root)
    quality_fixture(root)
    (root / 'bad.txt').unlink()
    tools = tmp_path / 'disappearing-tool'
    tools.mkdir()
    for name in ('git', 'uv'):
        (tools / name).symlink_to(shutil.which(name))
    rtk = tools / 'rtk'
    rtk.write_text('#!' + sys.executable + '\nimport os, pathlib, subprocess, sys\n'
                   'assert not any(k.startswith("OA_") and k != "OA_AUTH_ENV_FILE" for k in os.environ)\n'
                   'assert os.environ["HOME"] != ' + repr(os.environ['HOME']) + '\n'
                   'result = subprocess.run([' + repr(shutil.which('rtk')) + ', *sys.argv[1:]])\n'
                   'pathlib.Path(__file__).unlink()\nraise SystemExit(result.returncode)\n')
    rtk.chmod(0o700)
    result = run(root, 'verify', '--baseline', 'main', '--check', 'format', '--check', 'hygiene',
                 extra_env={'PATH':str(tools),'OA_APP_SECRET':'blocked'})
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report['checks'][0]['status'] == 'passed'
    assert report['checks'][1]['status'] == 'not_run'


@pytest.mark.parametrize('hook', ['sitecustomize.py', 'unsafe.pth'])
def test_preflight_never_executes_startup_hooks_or_loads_process_credentials(tmp_path, hook):
    root = checkout(tmp_path)
    venv(root)
    site = root / '.venv/lib/python3.11/site-packages'
    marker = tmp_path / 'startup-side-effect'
    (site / hook).write_text('import pathlib; pathlib.Path(' + repr(str(marker)) + ').write_text("must not execute")\n')
    result = run(root, 'preflight', '--baseline', 'main', extra_env={'OA_APP_SECRET':'synthetic-secret'})
    assert not marker.exists()
    assert result.returncode == 2
    assert 'python_startup_hooks_require_inspection' in json.loads(result.stdout)['reasons']
    assert 'synthetic-secret' not in result.stdout + result.stderr


def test_local_metadata_does_not_allow_external_package_files(tmp_path):
    root = checkout(tmp_path)
    venv(root)
    site = root / '.venv/lib/python3.11/site-packages'
    external = tmp_path / 'external-package'
    external.mkdir()
    (external / '__init__.py').write_text('')
    (site / 'syntheticpkg').symlink_to(external, target_is_directory=True)
    metadata = site / 'syntheticpkg-1.0.dist-info'
    metadata.mkdir()
    (metadata / 'METADATA').write_text('Metadata-Version: 2.1\nName: syntheticpkg\nVersion: 1.0\n')
    (metadata / 'RECORD').write_text('syntheticpkg/__init__.py,,\n')
    (root / 'requirements-dev.txt').write_text('syntheticpkg==1.0\n')
    result = run(root, 'preflight', '--baseline', 'main')
    assert result.returncode == 2
    assert 'shared_writable_dependencies' in json.loads(result.stdout)['reasons']


@pytest.mark.parametrize('damage', [None, 'pth', 'helper'])
def test_verified_uv_bootstrap_is_accepted_without_execution(tmp_path, damage):
    root = checkout(tmp_path)
    venv(root)
    site = root / '.venv/lib/python3.11/site-packages'
    stock = SCRIPT.parents[1] / 'tests/fixtures/uv-bootstrap'
    (site / '_virtualenv.pth').write_bytes((stock / 'bootstrap.pth.fixture').read_bytes())
    (site / '_virtualenv.py').write_bytes((stock / '_virtualenv.py.txt').read_bytes())
    if damage:
        path = site / ('_virtualenv.pth' if damage == 'pth' else '_virtualenv.py')
        path.write_bytes(path.read_bytes() + b'\n# modified\n')
    result = run(root, 'preflight', '--baseline', 'main')
    report = json.loads(result.stdout)
    assert result.returncode == (2 if damage else 0), result.stdout
    if damage:
        assert 'python_startup_hooks_require_inspection' in report['reasons']
    else:
        assert report['environment']['probe_without_site'] is True
    assert not (site / '__pycache__').exists()


def test_interpreter_failure_preserves_exit_and_raw_evidence(tmp_path):
    root = checkout(tmp_path)
    venv(root)
    interpreter = root / '.venv/bin/python'
    interpreter.unlink()
    interpreter.write_text('#!' + sys.executable + '\nimport os, sys\n'
                           'assert "OA_APP_SECRET" not in os.environ\n'
                           'print("synthetic probe output")\n'
                           'print("synthetic probe failure", file=sys.stderr)\n'
                           'raise SystemExit(23)\n')
    interpreter.chmod(0o700)
    result = run(root, 'preflight', '--baseline', 'main', extra_env={'OA_APP_SECRET':'not-in-probe'})
    report = json.loads(result.stdout)
    assert result.returncode == 2
    assert report['environment']['probe'] == {'exit_code':23, 'stdout':'synthetic probe output\n',
                                             'stderr':'synthetic probe failure\n'}
    assert 'not-in-probe' not in result.stdout + result.stderr


@pytest.mark.parametrize('outcome', ['active', 'failure'])
def test_full_validation_defers_before_checks_and_keeps_process_failure_evidence(tmp_path, outcome):
    root = checkout(tmp_path)
    venv(root)
    tools = tmp_path / 'process-adapter'
    tools.mkdir()
    ps = tools / 'ps'
    ps.write_text('#!' + sys.executable + '\nimport sys\n' +
                  ('print("python /synthetic/finance_preaudit_run.py")\n' if outcome == 'active' else
                   'print("synthetic ps failure", file=sys.stderr)\nraise SystemExit(29)\n'))
    ps.chmod(0o700)
    result = run(root, 'verify', '--baseline', 'main', '--full',
                 extra_env={'PATH':str(tools)+os.pathsep+os.environ['PATH']})
    report = json.loads(result.stdout)
    assert result.returncode == 2
    assert 'checks' not in report
    if outcome == 'active':
        assert 'active_controller_defer_full_validation' in report['reasons']
    else:
        assert report['failure_evidence']['exit_code'] == 29
        assert report['failure_evidence']['stderr'] == 'synthetic ps failure\n'
