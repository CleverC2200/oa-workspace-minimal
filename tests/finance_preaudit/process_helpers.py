from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

from apps import json_artifact
from apps.oa_approval import resource_slots
from apps.oa_finance_preaudit import flow_engine


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _codex_task(tmp_path: Path) -> tuple[dict, Path]:
    project_root = tmp_path / "project"
    action_file = (
        project_root
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
        / "cases"
        / ("a" * 64)
        / "attempt-1"
        / "action.json"
    )
    action_file.parent.mkdir(parents=True)
    json_artifact.write_json_atomic(
        action_file,
        {
            "schema_version": 1,
            "kind": "preaudit_action",
            "action": "review_required",
            "workflow_key": "travel_expense",
            "action_token": "test-action-token",
        },
    )
    json_artifact.write_json_atomic(
        action_file.with_name("review-packet.json"),
        {
            "workflow_key": "travel_expense",
            "rule_version": "travel_expense:1",
        },
    )
    return {
        "project_root": str(project_root),
        "action_file": str(action_file),
        "timeout_seconds": 900,
    }, action_file


def _install_fake_codex_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ignore_child_sigterm: bool,
) -> tuple[Path, Path, str]:
    pid_file = tmp_path / "codex-tree-pids.txt"
    marker = f"oa-test-process-tree-{time.monotonic_ns()}"
    bin_root = tmp_path / "bin"
    bin_root.mkdir()
    fake_codex = bin_root / "codex"
    child_program = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(15)"
        if ignore_child_sigterm
        else "import time; time.sleep(15)"
    )
    fake_codex.write_text(
        f"""#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", {child_program!r}, {marker!r}])
Path({str(pid_file)!r}).write_text(
    f"{{os.getpid()}}\\n{{child.pid}}\\n", encoding="utf-8"
)
sys.stdin.read()
time.sleep(15)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_root}{os.pathsep}{os.environ['PATH']}")
    return pid_file, fake_codex, marker


def _process_command(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _assert_fake_codex_tree_reaped(
    pid_file: Path,
    fake_codex: Path,
    marker: str,
) -> None:
    pids = [int(value) for value in pid_file.read_text(encoding="utf-8").split()]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(_pid_exists(pid) for pid in pids):
        time.sleep(0.02)
    remaining = [pid for pid in pids if _pid_exists(pid)]
    if remaining:
        ownership_verified = all(
            (
                str(fake_codex) in _process_command(pid)
                if pid == pids[0]
                else marker in _process_command(pid)
            )
            and os.getpgid(pid) == pids[0]
            for pid in remaining
        )
        if ownership_verified:
            os.killpg(pids[0], signal.SIGKILL)
    assert remaining == []


def _flow_action_worker(root: str) -> dict:
    def collect(request_id: str, _attempt_root: Path) -> dict:
        time.sleep(0.08)
        return {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": request_id,
            "workflow_key": "travel_expense",
            "workflow_name": "差旅费报销流程",
            "declared_fields": [{"field_name": "period", "value": "2026-08"}],
            "resources": [],
            "prepared": {
                "images": [],
                "structured_contents": [],
                "manifest": [],
            },
            "authority_facts": [],
            "warnings": [],
        }

    engine = flow_engine.FlowEngine(
        root=Path(root),
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-23T12:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=collect,
    )
    action = engine.next_action("travel_expense")
    return {
        "action": action["action"],
        "case_key": action.get("case_key"),
        "action_file": action.get("action_file"),
    }


def _slotted_worker(task: tuple[str, float, float]) -> dict:
    root, delay, startup_delay = task
    time.sleep(startup_delay)
    with resource_slots.acquire(
        "travel-browser",
        capacity=2,
        timeout_sec=2,
        root=Path(root),
    ):
        started_at = time.monotonic()
        (Path(root) / f"entered-{os.getpid()}").touch()
        deadline = time.monotonic() + 1
        while len(list(Path(root).glob("entered-*"))) < 2:
            if time.monotonic() >= deadline:
                raise TimeoutError("resource_slot_overlap_not_observed")
            time.sleep(0.005)
        time.sleep(delay)
        return {
            "started_at": started_at,
            "ended_at": time.monotonic(),
        }


def _interval_peak(intervals: list[tuple[float, float]]) -> int:
    active = 0
    peak = 0
    events = [
        event
        for started_at, ended_at in intervals
        for event in ((started_at, 1), (ended_at, -1))
    ]
    for _occurred_at, delta in sorted(events, key=lambda event: (event[0], event[1])):
        active += delta
        peak = max(peak, active)
    return peak
