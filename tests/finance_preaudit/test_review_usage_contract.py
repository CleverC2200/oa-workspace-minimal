from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from apps.oa_finance_preaudit import codex_review_worker as worker, flow_engine
from tests.finance_preaudit import review_profiles
from tests.finance_preaudit.test_flow_engine_submission import _material, _needs_human_decision


retained_deepseek_cli = review_profiles.retained_deepseek_cli


@pytest.fixture
def consume_usage(tmp_path, monkeypatch):
    project = tmp_path / "project"
    engine = flow_engine.FlowEngine(
        root=project / "runtime/finance_preaudit_flows",
        discover_candidates=lambda key: [{"request_id": "synthetic-usage", "workflow_key": key,
            "received_at": "2026-08-22T09:00:00+08:00"}],
        collect_material=lambda request, _: _material(request),
        submit_frozen=lambda *_: pytest.fail("usage verification must not submit"),
    )
    action = engine.next_action("travel_expense")
    path = Path(action["action_file"])
    original = {file: file.read_bytes() for file in path.parent.iterdir() if file.is_file()}
    decision = _needs_human_decision(action)
    script = """
import json, sys
from pathlib import Path
Path(sys.argv[1]).write_text(sys.argv[2])
print(json.dumps({'type': 'turn.started'}), flush=True)
print(sys.argv[3], flush=True)
"""
    monkeypatch.setenv("OA_REVIEW_API_KEY", "unit-test-key")
    monkeypatch.setattr(worker.runtime_control, "stop_requested", lambda: False)

    def consume(event):
        commands = []

        def command(_root, _schema, output, **_kwargs):
            commands.append(output)
            return [sys.executable, "-I", "-c", script, str(output), json.dumps(decision),
                    json.dumps({"type": "turn.completed", **event})]

        monkeypatch.setattr(worker, "_codex_command", command)
        result = worker.review({"project_root": str(project), "action_file": str(path), "timeout_seconds": 60})
        assert result["status"] == "reviewed" and result["error_code"] is None
        assert result["decision"] == decision and result["write_state"] == "not_requested"
        assert result["process_attempts"] == 1 and len(commands) == 1
        assert result["codex_events"]["status"] == "recorded"
        assert {file: file.read_bytes() for file in original} == original
        assert not path.with_name("execution-receipt.json").exists()
        return result["token_usage"]

    return consume


@pytest.mark.parametrize("event", [
    {},
    {"usage": None},
    {"usage": {}},
    {"usage": {"cached_input_tokens": 2, "output_tokens": 5}},
    {"usage": {"input_tokens": 10, "output_tokens": 5}},
    {"usage": {"input_tokens": 10, "cached_input_tokens": 2}},
])
def test_missing_usage_remains_unknown_without_rejecting_opinion(consume_usage, event):
    assert consume_usage(event) == {"status": "unknown"}


@pytest.mark.parametrize("value", [None, False, -1, 1.5, "10", float("nan"), float("inf")])
def test_invalid_usage_remains_unknown(consume_usage, value):
    assert consume_usage({"usage": {"input_tokens": value, "cached_input_tokens": 0,
                                    "output_tokens": 5}}) == {"status": "unknown"}


def test_cached_input_cannot_exceed_input(consume_usage):
    assert consume_usage({"usage": {"input_tokens": 10, "cached_input_tokens": 11,
                                    "output_tokens": 5}}) == {"status": "unknown"}


@pytest.mark.parametrize("usage", [
    {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
    {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5},
    {"input_tokens": 10, "cached_input_tokens": 10, "output_tokens": 5, "reasoning_output_tokens": 3},
])
def test_complete_usage_preserves_zero_and_counts_cache_once(consume_usage, usage):
    result = consume_usage({"usage": usage})
    assert result == {"status": "recorded", "input_tokens": usage["input_tokens"],
                      "cached_input_tokens": usage["cached_input_tokens"],
                      "output_tokens": usage["output_tokens"],
                      "total_tokens": usage["input_tokens"] + usage["output_tokens"]}
