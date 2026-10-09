from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_pytest_case(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / 'conftest.py').write_bytes((PROJECT_ROOT / 'tests/conftest.py').read_bytes())
    (tmp_path / 'test_case.py').write_text(source, encoding='utf-8')
    environment = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT),
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
