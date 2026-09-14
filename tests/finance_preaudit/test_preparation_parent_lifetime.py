from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from scripts.finance_preaudit_concurrency_probe import _preparation_executor


def _alive(pid: int) -> bool:
    value = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True,
    )
    return bool(value.stdout.strip()) and not value.stdout.strip().startswith("Z")


@pytest.mark.skipif(os.name != "posix", reason="Preparation groups require POSIX")
def test_preparation_group_exits_when_its_parent_dies(tmp_path):
    helper = tmp_path / "parent.py"
    receipt = tmp_path / "pids.txt"
    helper.write_text('''import os, pathlib, subprocess, sys, time
from scripts.finance_preaudit_concurrency_probe import _preparation_executor

def prepare(path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pathlib.Path(path).write_text(f"{os.getpid()} {child.pid}")
    time.sleep(60)

if __name__ == "__main__":
    pool = _preparation_executor()
    future = pool.submit(prepare, sys.argv[1])
    while not pathlib.Path(sys.argv[1]).exists():
        if future.done(): future.result()
        time.sleep(0.01)
    time.sleep(60)
''')
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root)}
    parent = subprocess.Popen(
        [sys.executable, str(helper), str(receipt)], cwd=root, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    worker = child = None
    try:
        deadline = time.monotonic() + 10
        while not receipt.exists() and time.monotonic() < deadline:
            assert parent.poll() is None
            time.sleep(0.05)
        worker, child = map(int, receipt.read_text().split())
        assert os.getpgid(worker) == worker
        assert os.getpgid(child) == worker
        time.sleep(0.2)
        assert _alive(worker) and _alive(child)
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while (_alive(worker) or _alive(child)) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(worker), "orphan preparation worker survived its parent"
        assert not _alive(child), "orphan preparation descendant survived its parent"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if worker and _alive(worker) and os.getpgid(worker) == worker:
            os.killpg(worker, signal.SIGKILL)
        elif child and _alive(child):
            os.kill(child, signal.SIGKILL)


def test_preparation_pool_still_completes_and_shuts_down():
    with _preparation_executor() as pool:
        worker = pool.submit(os.getpid).result(timeout=10)
        assert worker != os.getpid()
        assert _alive(worker)
    assert not _alive(worker)
