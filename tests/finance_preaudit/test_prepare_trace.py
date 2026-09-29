from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import pytest

from apps.oa_finance_preaudit import prepare_trace as trace
from scripts import finance_preaudit_concurrency_probe as probe


def context(task="b" * 32):
    return {"trace_id": "a" * 32, "task_id": task, "task_index": 0,
            "run_id": "c" * 32, "controller_instance_id": "d" * 32}


def test_real_prepare_stages_and_redacted_sample(tmp_path, monkeypatch):
    stages = []
    original_stage = trace.stage

    def stage(name):
        stages.append(name)
        original_stage(name)

    monkeypatch.setattr(trace, "stage", stage)
    monkeypatch.setattr(probe.runtime_control, "stop_requested", lambda: False)
    monkeypatch.setattr(probe, "_historical_write_authorization_failures", lambda: set())
    monkeypatch.setattr(probe, "_local_quarantine_count", lambda: 0)

    def pending():
        # Synthetic business text must never be serialized, even while on the stack.
        private_value = "DO_NOT_RECORD_BUSINESS_OR_CREDENTIAL_TEXT"
        deadline = time.monotonic() + 3
        target = tmp_path / ("prepare-progress-" + "a" * 32 + ".json")
        while time.monotonic() < deadline:
            if target.exists() and json.loads(target.read_text())["stage"] == "live_pending_read":
                assert private_value not in target.read_text()
                return []
            time.sleep(0.01)
        raise AssertionError("no live pending snapshot")

    monkeypatch.setattr(probe.cli, "_live_pending_identities", pending)
    result = trace.run(probe._prepare_one, "*", tmp_path, context())
    assert result["action"] == "no_action"
    sample = json.loads(next(tmp_path.glob("prepare-progress-*.json")).read_text())
    assert sample["stage"] == "live_pending_read"
    assert any(f["function"] == "_prepare_one" for f in sample["stack"])
    assert all(not Path(f["file"]).is_absolute() for f in sample["stack"])
    assert "engine_build" not in stages
    assert stages == ["selection_contract", "quarantine_probe", "live_pending_read",
                      "identity_contract", "recovery_candidates"]
    assert not list(tmp_path.glob("prepare-timeout-*.json"))
    assert trace._active is None


def test_phase_totals_are_bounded_and_repeatable(tmp_path, monkeypatch):
    current = trace.Trace(tmp_path, context(), threading.get_ident(), started=1,
                          phase=("selection_contract", 1, {}))
    monkeypatch.setattr(trace._local, "trace", current, raising=False)
    monkeypatch.setattr(trace.time, "monotonic", lambda: 3)
    trace.stage("live_pending_read")
    monkeypatch.setattr(trace.time, "monotonic", lambda: 5)
    trace.stage("selection_contract")
    monkeypatch.setattr(trace.time, "monotonic", lambda: 6)
    sample = current.snapshot()
    assert sample["stage_totals_seconds"] == {"selection_contract": 3, "live_pending_read": 2}
    assert sample["elapsed_seconds"] == 5
    trace.stage("DO_NOT_RECORD_BUSINESS_TEXT")
    assert current.snapshot()["stage"] == "selection_contract"


@pytest.mark.parametrize("damage", ["stale", "corrupt", "oversized", "extra", "external", "future", "huge_number", "nested"])
def test_timeout_snapshot_rejects_stale_or_unsafe_data(tmp_path, damage):
    current = trace.Trace(tmp_path, context(), threading.get_ident())
    sample = current.snapshot()
    if damage == "stale":
        sample["task_id"] = "e" * 32
    elif damage == "extra":
        sample["private_body"] = "DO_NOT_RECORD_BUSINESS_TEXT"
    elif damage == "external":
        sample["stack"] = [{"file": "/private/data.py", "function": "secret", "line": 1}]
    elif damage == "huge_number":
        sample["elapsed_seconds"] = 10 ** 400
    elif damage == "future":
        sample["sampled_monotonic"] += 1000
    path = tmp_path / ("prepare-progress-" + "a" * 32 + ".json")
    raw = json.dumps(sample)
    if damage == "corrupt":
        raw = "{"
    elif damage == "oversized":
        raw = " " * 16385
    elif damage == "nested":
        raw = "[" * 2000 + "]" * 2000
    path.write_text(raw)
    trace.record_timeout(tmp_path, context())
    receipt = json.loads(next(tmp_path.glob("prepare-timeout-*.json")).read_text())
    assert "DO_NOT_RECORD_BUSINESS_TEXT" not in json.dumps(receipt)
    assert receipt["error_code"] == "production_probe_prepare_timeout"
    assert receipt["sample_status"] == ("recorded" if damage == "extra" else "unavailable")


def test_diagnostic_io_failure_keeps_real_prepare_result(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("private error text")

    monkeypatch.setattr(trace, "_write", fail)
    calls = []
    result = trace.run(lambda arg: calls.append(arg) or {"action": "no_action"},
                       "private selection", tmp_path, context())
    trace.record_timeout(tmp_path, context())
    assert result == {"action": "no_action"}
    assert calls == ["private selection"]
    assert trace._active is None
    assert not list(tmp_path.iterdir())


def test_sampler_start_failure_does_not_skip_or_repeat_preparation(tmp_path, monkeypatch):
    monkeypatch.setattr(trace, "_sampler", None)
    monkeypatch.setattr(threading.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError()))
    calls = []
    assert trace.run(lambda arg: calls.append(arg) or {"action": "no_action"},
                     "selection", tmp_path, context()) == {"action": "no_action"}
    assert calls == ["selection"]
    assert trace._active is None


def test_trace_reuses_one_sampler_and_clears_on_exception(tmp_path):
    first = []
    trace.run(lambda arg: first.append(trace._sampler), None, tmp_path, context())
    with pytest.raises(ValueError, match="original_failure"):
        trace.run(lambda arg: (_ for _ in ()).throw(ValueError("original_failure")),
                  None, tmp_path, context("e" * 32))
    assert trace._sampler is first[0]
    assert trace._active is None


def test_diagnostic_symlinks_do_not_overwrite_other_files(tmp_path):
    target = tmp_path / "preserved.json"
    target.write_text("preserve")
    (tmp_path / ("prepare-timeout-" + "a" * 32 + ".json")).symlink_to(target)
    trace.record_timeout(tmp_path, context())
    assert target.read_text() == "preserve"
