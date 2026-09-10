from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import flow_engine, session_alert


WORKFLOW = "packaging_layout_approval"


def _failed_case(root: Path, workflow: str = WORKFLOW):
    pending = [{
        "request_id": "stale-material-fixture",
        "received_at": "2026-09-02T18:29:00+08:00",
        "workflow_key": workflow,
    }]
    handled = [{"requestId": pending[0]["request_id"]}]
    collected = []

    def collect(request_id, _attempt_root):
        collected.append(request_id)
        raise RuntimeError("oa_session_expired")

    def unexpected_write(*_args):
        pytest.fail("material failure must not reach authorization or submission")

    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda _key: list(pending),
        discover_handled=lambda: list(handled),
        collect_material=collect,
        authorization_source=unexpected_write,
        authorization_verifier=unexpected_write,
        submit_frozen=unexpected_write,
    )
    failed = engine.next_action(workflow)
    assert failed["reason"] == "material_collection_failed"
    assert failed["failure_detail_code"] == "oa_session_expired"
    return engine, pending, handled, collected, failed


@pytest.mark.parametrize("mode", ["preview", "controlled_write"])
@pytest.mark.parametrize("legacy_receipt", ["present", "missing", "invalid"])
def test_empty_packaging_queue_does_not_reproject_handled_material_failure(tmp_path, mode, legacy_receipt):
    engine, pending, _handled, collected, failed = _failed_case(tmp_path)
    pending.clear()
    attempt = Path(failed["action_file"]).parent
    receipt = attempt / "manual-review-receipt.json"
    if legacy_receipt == "missing":
        receipt.unlink(missing_ok=True)
    elif legacy_receipt == "invalid":
        receipt.write_text("{}")
    originals = {path: path.read_bytes() for path in attempt.glob("*.json")}

    for _ in range(2):
        result = engine.next_action(WORKFLOW, mode=mode)
        assert result["action"] == "closed"
        assert result["write_state"] == "not_requested"
        assert "reason" not in result

    assert len(collected) == 1
    assert list(attempt.parent.glob("attempt-*")) == [attempt]
    assert all(path.read_bytes() == data for path, data in originals.items())


@pytest.mark.parametrize("scenario", ["pending", "invisible", "handled_error", "malformed_handled", "conflicting_handled", "no_handled_port"])
def test_material_failure_requires_empty_pending_and_trusted_handled_lookup(tmp_path, scenario):
    engine, pending, handled, collected, failed = _failed_case(tmp_path)
    if scenario != "pending":
        pending.clear()
    if scenario == "invisible":
        handled.clear()
    elif scenario == "handled_error":
        def unavailable():
            raise RuntimeError("handled lookup unavailable")
        engine._discover_handled = unavailable
    elif scenario == "malformed_handled":
        handled.append(None)
    elif scenario == "conflicting_handled":
        handled[0]["request_id"] = "different-fixture"
    elif scenario == "no_handled_port":
        engine._discover_handled = None

    result = engine.next_action(WORKFLOW)

    assert result["action"] == "needs_admin"
    assert result["action_file"] == failed["action_file"]
    assert len(collected) == 1


@pytest.mark.parametrize("artifact", [
    "writer-boundary.json", "execution-receipt.json", "review-decision.json",
    "remark.txt", "submission-envelope.json",
])
@pytest.mark.parametrize("symlink", [False, True])
def test_empty_packaging_queue_keeps_material_failure_with_boundary_evidence(tmp_path, artifact, symlink):
    engine, pending, _handled, collected, failed = _failed_case(tmp_path)
    pending.clear()
    path = Path(failed["action_file"]).with_name(artifact)
    if symlink:
        path.symlink_to(tmp_path / "missing-boundary")
    else:
        path.write_text("{}")

    result = engine.next_action(WORKFLOW)

    assert result["action"] == "needs_admin"
    assert len(collected) == 1


@pytest.mark.parametrize("mutation", ["unknown", "identity", "detail", "authorization", "other_failure"])
def test_empty_packaging_queue_keeps_untrusted_or_other_failed_actions(tmp_path, mutation):
    engine, pending, _handled, collected, failed = _failed_case(tmp_path)
    pending.clear()
    for name in ("action_file", "state_file"):
        path = Path(failed[name])
        payload = json.loads(path.read_text())
        if mutation == "unknown":
            payload.update(write_state="unknown", reason="writer_result_unknown")
        elif mutation == "other_failure":
            payload["reason"] = "codex_review_failed"
        elif name == "action_file":
            key, value = {
                "identity": ("case_key", "incorrect-case"),
                "detail": ("failure_detail_code", "attachment_download_failed"),
                "authorization": ("write_authorized", True),
            }[mutation]
            payload[key] = value
        path.write_text(json.dumps(payload))

    result = engine.next_action(WORKFLOW)

    assert result["action"] == "needs_admin"
    assert len(collected) == 1
    if mutation == "unknown":
        assert result["write_state"] == "unknown"


def test_empty_finance_queue_retains_existing_recovery_contract(tmp_path):
    engine, pending, _handled, collected, failed = _failed_case(tmp_path, "travel_expense")
    pending.clear()

    assert engine.next_action("travel_expense")["action_file"] == failed["action_file"]
    assert len(collected) == 1


@pytest.mark.parametrize("field", ["action_file", "state_file"])
def test_empty_packaging_queue_keeps_linked_persistent_state(tmp_path, field):
    engine, pending, _handled, collected, failed = _failed_case(tmp_path / "flows")
    pending.clear()
    path = Path(failed[field])
    original = path.read_bytes()
    outside = tmp_path / "outside-attempt.json"
    outside.write_bytes(original)
    path.unlink()
    path.symlink_to(outside)

    result = engine.next_action(WORKFLOW)

    assert result["action"] == "needs_admin"
    assert outside.read_bytes() == original
    assert len(collected) == 1


def test_empty_packaging_queue_keeps_current_completion_lock(tmp_path):
    engine, pending, _handled, collected, failed = _failed_case(tmp_path)
    pending.clear()
    with flow_engine._exclusive_lock(Path(failed["action_file"]).with_name("complete.lock")):
        with pytest.raises(ValueError, match="completion_in_progress"):
            engine.next_action(WORKFLOW)
    assert len(collected) == 1


def test_handled_lookup_does_not_dismiss_failure_when_pending_discovery_fails(tmp_path):
    engine, _pending, _handled, collected, failed = _failed_case(tmp_path)

    def unavailable(_workflow):
        raise RuntimeError("pending lookup unavailable")

    engine._discover_candidates = unavailable
    assert engine.next_action(WORKFLOW)["action_file"] == failed["action_file"]
    assert len(collected) == 1


@pytest.mark.parametrize("live_failure", [None, "rpa_session_missing"])
def test_material_alert_cli_preserves_session_detail_and_rechecks_current_session(tmp_path, monkeypatch, capsys, live_failure):
    _engine, _pending, _handled, _collected, failed = _failed_case(tmp_path / "flows")
    calls = {"live": 0, "send": []}

    def live():
        calls["live"] += 1
        if live_failure:
            raise RuntimeError(live_failure)
        return {"status": "ok"}

    monkeypatch.setattr(session_alert, "_live_status", live)
    monkeypatch.setattr(session_alert.auth, "load_local_env", lambda: None)
    monkeypatch.setattr(session_alert, "send_message", lambda *args, **_kwargs: calls["send"].append(args))
    result = session_alert.main([
        "--error-code", failed["reason"],
        "--failure-detail-code", failed["failure_detail_code"],
        "--confirm-send", "YES", "--control-root", str(tmp_path / "alerts"),
    ])

    assert result == 0
    receipt = json.loads(capsys.readouterr().out)
    assert calls["live"] == 1
    assert receipt["error_code"] == (live_failure or "oa_session_expired")
    assert receipt["delivery_state"] == ("sent" if live_failure else "not_required")
    assert len(calls["send"]) == bool(live_failure)


def test_material_alert_detail_cannot_override_non_material_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(session_alert, "alert_from_environment", lambda *_args, **_kwargs: pytest.fail("invalid detail must not send"))
    result = session_alert.main([
        "--error-code", "writer_result_unknown", "--failure-detail-code", "oa_session_expired",
        "--confirm-send", "YES", "--control-root", str(tmp_path),
    ])
    assert result == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "feishu_alert_failed"


def test_material_alert_detail_still_requires_explicit_send_confirmation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(session_alert, "alert_from_environment", lambda *_args, **_kwargs: pytest.fail("unconfirmed alert must not send"))
    result = session_alert.main([
        "--error-code", "material_collection_failed", "--failure-detail-code", "oa_session_expired",
        "--control-root", str(tmp_path),
    ])
    assert result == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "feishu_alert_confirmation_missing"
