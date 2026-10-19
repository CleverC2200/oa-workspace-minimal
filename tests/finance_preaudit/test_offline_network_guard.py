from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_pytest_case(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / 'conftest.py').write_bytes((PROJECT_ROOT / 'tests/conftest.py').read_bytes())
    (tmp_path / 'test_case.py').write_text(source, encoding='utf-8')
    inherited = (
        'PATH', 'USER', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ',
        'SYSTEMROOT', 'WINDIR', 'OA_FINANCE_PREAUDIT_STOP_FILE',
        'OA_FINANCE_PREAUDIT_STOP_REQUESTED', 'OA_FROZEN_WRITER_LOCK_FILE',
        'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', 'OA_APPROVAL_OWNER_TASK_ID',
        'OA_APPROVAL_RUN_ID', 'OA_APPROVAL_INSTANCE_ID', 'OA_APPROVAL_EXECUTION_OWNERS',
        'OA_CONTROLLER_EXECUTION_MANIFEST_FILE',
    )
    environment = {name: os.environ[name] for name in inherited if name in os.environ}
    home = tmp_path / 'synthetic-home'
    home.mkdir(mode=0o700)
    auth_file = home / 'auth.env'
    auth_file.touch(mode=0o600)
    environment.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
                       OA_AUTH_ENV_FILE=str(auth_file), PYTHONPATH=str(PROJECT_ROOT),
                       PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    return subprocess.run(
        [sys.executable, '-B', '-m', 'pytest', '-q', '--tb=short',
         '--confcutdir', str(tmp_path), '--rootdir', str(tmp_path), str(tmp_path)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
    )


def test_omitted_invoice_client_fails_before_transport_even_if_business_failure_is_expected(tmp_path):
    # The past replay omitted invoice_client. The sentinel transport makes the red
    # run safe too: reaching it records the mistake but never opens a connection.
    result = run_pytest_case(tmp_path, '''
import socket
from pathlib import Path
from apps.oa_approval.invoice_buyers import read_invoice_buyers

def sentinel_connect(*args, **kwargs):
    Path('transport-reached').touch()
    raise OSError('recorded transport sentinel')

socket.getaddrinfo = lambda *args, **kwargs: [
    (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 1))
]
socket.socket.connect = sentinel_connect

class RecordedOa:
    def get(self, *args, **kwargs):
        return {'status': '1', 'token': 'synthetic-token', 'targetIds': 'synthetic-fid'}

def test_replay():
    data = {'workflowDetailTableInfos': [{'workflowRequestTableRecords': [{
        'workflowRequestTableFields': [{
            'fieldName': 'glfp', 'fieldShowName': '关联发票',
            'fieldValue': '101', 'fieldShowValue': '00000000000000000101',
        }]
    }]}]}
    result = read_invoice_buyers(RecordedOa(), data)
    assert result['read_status'] == 'failed'
''')
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'offline_network_forbidden' in result.stdout
    assert 'explicit recorded/fake transport' in result.stdout
    assert not (tmp_path / 'transport-reached').exists()


def test_threaded_ipv4_and_ipv6_transport_is_rejected_without_dns(tmp_path):
    result = run_pytest_case(tmp_path, '''
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

def sentinel_connect(*args, **kwargs):
    Path('transport-reached').touch()

socket.socket.connect = sentinel_connect

@pytest.mark.parametrize('family,address', [
    (socket.AF_INET, ('127.0.0.1', 1)),
    (socket.AF_INET6, ('::1', 1)),
])
def test_transport(family, address):
    with socket.socket(family) as channel, ThreadPoolExecutor(max_workers=1) as workers:
        workers.submit(channel.connect, address).result()
''')
    assert result.returncode == 1, result.stdout + result.stderr
    failures = [line for line in result.stdout.splitlines()
                if line.startswith('offline_network_forbidden:')]
    assert len(failures) == 2, result.stdout + result.stderr
    assert '2 failed' in result.stdout
    assert not (tmp_path / 'transport-reached').exists()


def test_explicit_http_fake_and_local_ipc_remain_available(tmp_path):
    result = run_pytest_case(tmp_path, '''
import socket
import urllib.request

def test_recorded_response(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'recorded-response'
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs: Response())
    with urllib.request.urlopen('https://recorded.invalid/example') as response:
        assert response.read() == b'recorded-response'

def test_local_ipc(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    address = 'ipc'
    with socket.socket(socket.AF_UNIX) as listener, socket.socket(socket.AF_UNIX) as client:
        listener.bind(address)
        listener.listen(1)
        client.connect(address)
        connection, _ = listener.accept()
        with connection:
            client.sendall(b'local-ipc')
            assert connection.recv(32) == b'local-ipc'
''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert '2 passed' in result.stdout


def test_child_collection_uses_synthetic_auth_without_parent_credentials(tmp_path, monkeypatch):
    parent_home = tmp_path / 'parent-home'
    parent_home.mkdir()
    parent_auth = parent_home / 'auth.env'
    parent_auth.write_text('OA_AUTH_FILE_SENTINEL=parent-file-only\n', encoding='utf-8')
    parent_auth.chmod(0o600)
    monkeypatch.setenv('HOME', str(parent_home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(parent_home / 'config'))
    monkeypatch.setenv('CODEX_HOME', str(parent_home / 'codex'))
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(parent_auth))
    for name in ('OA_BASE_URL', 'OA_APP_ID', 'OA_APP_SECRET', 'OA_UID', 'OA_REVIEW_API_KEY',
                 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'ANTHROPIC_API_KEY', 'CODEX_API_KEY'):
        monkeypatch.setenv(name, 'synthetic-parent-secret')
    preserved = {
        'OA_APPROVAL_OWNER_TASK_ID': '00000000-0000-4000-8000-000000000001',
        'OA_APPROVAL_RUN_ID': '00000000000040008000000000000002',
        'OA_APPROVAL_INSTANCE_ID': '00000000000040008000000000000003',
        'OA_APPROVAL_EXECUTION_OWNERS': '[{"pid":12345,"start_time":"synthetic"}]',
        'OA_CONTROLLER_EXECUTION_MANIFEST_FILE': str(tmp_path / 'execution.json'),
        'OA_FINANCE_PREAUDIT_STOP_FILE': str(tmp_path / 'stop.flag'),
        'OA_FROZEN_WRITER_LOCK_FILE': str(tmp_path / 'writer.lock'),
        'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE': str(tmp_path / 'controller.lock'),
    }
    for name, value in preserved.items():
        monkeypatch.setenv(name, value)
    result = run_pytest_case(tmp_path, '''
import os
from pathlib import Path
from apps.oa_approval import attachments, auth

assert auth.configuration_status() == {'status': 'missing', 'configured': 0, 'required': 4, 'source': 'none'}
assert 'OA_AUTH_FILE_SENTINEL' not in os.environ
assert not any(os.environ.get(name) for name in (
    'OA_BASE_URL', 'OA_APP_ID', 'OA_APP_SECRET', 'OA_UID', 'OA_REVIEW_API_KEY',
    'OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'ANTHROPIC_API_KEY', 'CODEX_API_KEY'))
assert 'CODEX_HOME' not in os.environ
assert Path.home() != Path(PARENT_HOME)
assert os.environ['XDG_CONFIG_HOME'] != PARENT_CONFIG
assert Path(os.environ['OA_AUTH_ENV_FILE']).read_bytes() == b''
assert Path(os.environ['OA_AUTH_ENV_FILE']).stat().st_mode & 0o777 == 0o600
assert {name: os.environ.get(name) for name in PRESERVED} == PRESERVED

def test_import_completed():
    assert attachments.OA_HOST == ''
'''.replace('PARENT_HOME', repr(str(parent_home))).replace(
        'PARENT_CONFIG', repr(str(parent_home / 'config'))).replace('PRESERVED', repr(preserved)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert '1 passed' in result.stdout
    assert parent_auth.read_text() == 'OA_AUTH_FILE_SENTINEL=parent-file-only\n'
