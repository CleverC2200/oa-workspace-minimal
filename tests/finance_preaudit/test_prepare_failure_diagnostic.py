from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import prepare_failure


def _raise_secret_error() -> None:
    raise TimeoutError("account=private-account token=private-secret")


def _capture_error() -> BaseException:
    try:
        _raise_secret_error()
    except BaseException as error:  # noqa: BLE001 - test helper
        return error
    raise AssertionError("unreachable")


def test_build_keeps_only_bounded_redacted_fields() -> None:
    error = _capture_error()
    payload = prepare_failure.build(
        stage="live_pending_read",
        error_code="production_probe_prepare_failed",
        error=error,
        stop_action="cycle_stop",
        run_id="0123456789abcdef0123456789abcdef",
        controller_instance_id="fedcba9876543210fedcba9876543210",
        workflow_key="travel_expense",
        case_key="a" * 64,
        attempt=2,
        action_file="/safe/runtime/finance_preaudit_flows/travel_expense/cases/"
        + "a" * 64
        + "/attempt-2/action.json",
    )

    assert payload["kind"] == "preaudit_prepare_failure_diagnostic"
    assert payload["stage"] == "live_pending_read"
    assert payload["error_code"] == "production_probe_prepare_failed"
    assert payload["reason_code"] == "production_probe_prepare_failed"
    assert payload["error_class"] == "TimeoutError"
    assert payload["last_frame"] == "_raise_secret_error"
    assert payload["stop_action"] == "cycle_stop"
    assert payload["item"] == {
        "workflow_key": "travel_expense",
        "case_key": "a" * 64,
        "attempt": 2,
        "action_file": payload["item"]["action_file"],
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "private-account" not in serialized
    assert "private-secret" not in serialized
    assert "account=" not in serialized


def test_build_rejects_unknown_stage_and_unstable_codes() -> None:
    with pytest.raises(ValueError):
        prepare_failure.build(stage="somewhere", error_code="production_probe_prepare_failed")
    with pytest.raises(ValueError):
        prepare_failure.build(stage="next_action", error_code="Not A Code")
    with pytest.raises(ValueError):
        prepare_failure.build(
            stage="next_action",
            error_code="production_probe_prepare_failed",
            stop_action="kill",
        )


def test_build_drops_unbounded_item_fields() -> None:
    payload = prepare_failure.build(
        stage="selection_contract",
        error_code="production_probe_prepare_failed",
        workflow_key="NOT-A-WORKFLOW",
        case_key="not-hex",
        attempt=0,
        action_file="relative/action.json",
    )

    assert payload["item"] is None
    assert payload["error_class"] is None
    assert payload["last_frame"] is None
    assert payload["run_id"] is None


def test_record_is_digest_named_and_idempotent(tmp_path: Path) -> None:
    kwargs = {
        "stage": "child_exception",
        "error_code": "production_probe_prepare_failed",
        "error": _capture_error(),
        "stop_action": "cycle_stop",
        "observed_at": datetime(2026, 9, 21, 1, 30, tzinfo=timezone.utc),
    }

    first = prepare_failure.record(tmp_path, **kwargs)
    second = prepare_failure.record(tmp_path, **kwargs)

    assert first == second
    assert first.parent == tmp_path
    assert first.name.startswith("prepare-failure-")
    assert json.loads(first.read_text()) == prepare_failure.build(**kwargs)
    assert len(list(tmp_path.glob("prepare-failure-*.json"))) == 1
    assert list(tmp_path.glob("*.tmp")) == []

    other = prepare_failure.record(tmp_path, **{**kwargs, "stage": "engine_build"})
    assert other != first
    assert len(list(tmp_path.glob("prepare-failure-*.json"))) == 2


def test_record_refuses_symlinked_targets(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError):
        prepare_failure.record(
            link,
            stage="engine_build",
            error_code="production_probe_prepare_failed",
        )
