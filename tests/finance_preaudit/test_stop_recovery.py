from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import reconciliation, stop_recovery


REQUEST_ID = "resolved-unknown-request"
RECEIVED_AT = "2026-09-19 09:59:37"


def _live(reconciled: dict) -> dict:
    return {
        "live_check": {
            "credentials": {"status": "ok"},
            "oa": {"execution_identity": "verified"},
        },
        "reconciliation": reconciled,
    }


def _trusted(item_key: str, category: str) -> dict:
    return {
        "continuation": {
            "status": "ready",
            "scope": "none",
            "isolated_pending": 0,
            "blocking_reasons": {},
        },
        "quarantine": {"pending_quarantined": 0},
        "manual_review": {"items": [{"item_key": item_key, "category": category}]},
    }


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / "flows"
    control = tmp_path / "control"
    control.mkdir()
    case_key = hashlib.sha256(
        f"{REQUEST_ID}\x1f{RECEIVED_AT}".encode("utf-8")
    ).hexdigest()
    attempt_root = root / "travel_expense" / "cases" / case_key / "attempt-1"
    attempt_root.mkdir(parents=True)
    action_file = attempt_root / "action.json"
    state_file = attempt_root / "state.json"
    state = {
        "schema_version": 1,
        "kind": "preaudit_action",
        "workflow_key": "travel_expense",
        "case_key": case_key,
        "attempt": 1,
        "request_id": REQUEST_ID,
        "received_at": RECEIVED_AT,
        "created_at": "2026-09-19T02:39:24.584149+00:00",
        "execution_state": "needs_admin",
        "business_state": "not_available",
        "write_state": "unknown",
        "reason": "writer_result_unknown",
        "action": "needs_admin",
        "action_file": str(action_file),
        "state_file": str(state_file),
    }
    for path in (action_file, state_file):
        path.write_text(json.dumps(state), encoding="utf-8")
    (attempt_root / "writer-boundary.json").write_text("{}", encoding="utf-8")
    stop_file = control / "stop-preaudit.flag"
    stop_file.write_text("stop_requested:writer_result_unknown\n", encoding="utf-8")
    os.chmod(stop_file, 0o600)
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_FLOW_ROOT", str(root))
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_FILE", str(stop_file))
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE", str(control / "controller.lock")
    )
    monkeypatch.setenv("OA_FROZEN_WRITER_LOCK_FILE", str(control / "writer.lock"))
    monkeypatch.delenv("OA_FINANCE_PREAUDIT_STOP_REQUESTED", raising=False)
    item_key = reconciliation._item_key(REQUEST_ID, RECEIVED_AT)
    return action_file, stop_file, item_key


def _recover(action_file: Path, snapshot: dict) -> dict:
    return stop_recovery.recover(
        confirmed=True,
        reason="writer_result_unknown",
        action_file=action_file,
        check_live=lambda: snapshot,
    )


def test_resolved_unknown_write_stop_is_cleared_with_receipt(environment) -> None:
    action_file, stop_file, item_key = environment
    result = _recover(action_file, _live(_trusted(item_key, "handled_not_pending")))
    assert result["status"] == "recovered"
    assert result["write_state"] == "not_requested"
    assert not stop_file.exists()
    receipt = json.loads(Path(result["receipt_file"]).read_text(encoding="utf-8"))
    assert receipt["kind"] == "preaudit_writer_stop_recovery"
    assert receipt["reason"] == "writer_result_unknown"
    assert receipt["bound_action_file"] == str(action_file)
    assert receipt["bound_write_state"] == "unknown"
    assert receipt["continuation"]["status"] == "ready"


def test_pending_unknown_write_keeps_the_stop(environment) -> None:
    action_file, stop_file, item_key = environment
    with pytest.raises(ValueError, match="writer_stop_recovery_blocked"):
        _recover(action_file, _live(_trusted(item_key, "pending_not_handled")))
    assert stop_file.exists()


def test_untrusted_reconciliation_keeps_the_stop(environment) -> None:
    action_file, stop_file, item_key = environment
    reconciled = _trusted(item_key, "handled_not_pending")
    reconciled["continuation"]["blocking_reasons"] = {
        "pending_write_state_unresolved": 1
    }
    reconciled["continuation"]["status"] = "blocked"
    reconciled["continuation"]["scope"] = "controller"
    with pytest.raises(ValueError, match="writer_stop_recovery_blocked"):
        _recover(action_file, _live(reconciled))
    assert stop_file.exists()


@pytest.mark.parametrize(
    "changed", ["wrong_reason", "env_stop", "loose_mode", "receipt_present"]
)
def test_untrusted_stop_mark_or_evidence_keeps_the_stop(
    environment, monkeypatch, changed
) -> None:
    action_file, stop_file, item_key = environment
    if changed == "wrong_reason":
        stop_file.write_text("stop_requested:controller_cycle_stop\n", encoding="utf-8")
        os.chmod(stop_file, 0o600)
    elif changed == "env_stop":
        monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_REQUESTED", "1")
    elif changed == "loose_mode":
        os.chmod(stop_file, 0o644)
    else:
        (action_file.parent / "execution-receipt.json").write_text(
            "{}", encoding="utf-8"
        )
    with pytest.raises(ValueError, match="writer_stop_recovery_blocked"):
        _recover(action_file, _live(_trusted(item_key, "handled_not_pending")))
    assert stop_file.exists()


def test_verified_postcheck_timeout_can_release_historical_stop(
    environment, monkeypatch
):
    from apps.oa_finance_preaudit import production_write

    action, stop, key = environment
    state_file = action.with_name("state.json")
    state = json.loads(state_file.read_text())
    state["reason"] = "writer_postcheck_timeout"
    state_file.write_text(json.dumps(state))
    observed = []

    def verified(attempt, value, code):
        observed.append((attempt, value["reason"], code))
        return True

    monkeypatch.setattr(production_write, "verified_unknown_write_failure", verified)
    before = {p: p.read_bytes() for p in action.parent.iterdir() if p.is_file()}
    result = _recover(action, _live(_trusted(key, "handled_not_pending")))
    assert result["status"] == "recovered"
    assert observed and all(
        x == (action.parent, "writer_postcheck_timeout", "writer_postcheck_timeout")
        for x in observed
    )
    assert not stop.exists()
    assert all(p.read_bytes() == raw for p, raw in before.items())
